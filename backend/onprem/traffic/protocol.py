"""Offline batch planning and evidence reduction for real pinned-sidecar traffic.

This module does not enable the scenario gate or claim a 50-UE capability.
Each UE needs two real overlapping five-second UDP measurements. Aggregate
offered load is a calibrated lab workload, not a radio/SLA capacity claim.
"""
from dataclasses import asdict, dataclass, replace
import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Condition, Event, Lock
from contextlib import contextmanager
import ipaddress
import json
import logging
import math
import re
import time

from backend.onprem.policy.errors import PolicyBlocked
from backend.onprem.traffic.capability import receiver_summary, sender_summary
from backend.onprem.traffic.planning import direction_rate, valid_rate, rate_budget
from backend.onprem.traffic.worker import (
    SCHEMA as DETACHED_SCHEMA, SCRIPT as DETACHED_WORKER_SCRIPT,
    command as detached_worker_command,
)


_POOLS = {name: ipaddress.ip_network(f"10.{101+i}.0.0/21")
          for i, name in enumerate(("embb", "urllc", "mmtc", "v2x"))}
_ENDPOINTS = {name: f"172.22.{61+i}.20" for i, name in enumerate(_POOLS)}
# Fixed kubeadm runtime N6 addresses: renderer configures MASQUERADE from each
# UE pool via its n6 interface. Independently verified deployed Pod annotations
# and commands; never learn an accepted peer from the iperf report itself.
_SNAT_PEERS = {name: f"172.22.{61+i}.10" for i, name in enumerate(_POOLS)}
_NSSAI = {'embb': (1, '000001'), 'urllc': (2, '000002'), 'mmtc': (3, '000004'), 'v2x': (4, '000005')}
_UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
LEGACY_PROFILE = 'scenario-5s-v1'

SPAN_NAMES = frozenset(('receiver_readiness','client_readiness','immutable_checkpoint',
                        'native_postflight','cancel_sweep1','worker_join','cancel_sweep2',
                        'cleanup','initial_reduce'))

def validate_batch_spans(value):
    """Optional unvalidated timing diagnostics, never freshness/closure proof."""
    def finite_offset(v):
        try:return type(v) in (int,float) and math.isfinite(v) and v>=0
        except (OverflowError,ValueError):return False
    if (not isinstance(value,dict) or set(value)!={'schema','clockAvailable','spans'}
            or value['schema']!='cityverse.batch-spans/v1' or type(value['clockAvailable']) is not bool
            or not isinstance(value['spans'],dict) or set(value['spans'])-SPAN_NAMES):
        return False
    for row in value['spans'].values():
        if not isinstance(row,dict) or set(row)!={'start','end'}: return False
        start,end=row['start'],row['end']
        if any(v is not None and not finite_offset(v) for v in (start,end)): return False
        if end is not None and (start is None or end<start): return False
        if start is None and value['clockAvailable']: return False
    return len(json.dumps(value,allow_nan=False).encode())<=1024

class _BatchSpans:
    """One monotonic clock, locked across both threads; absent/end-null is unavailable."""
    def __init__(self, clock):
        self.clock=clock;self.lock=Lock();self.origin=None;self.last=None
        self.available=True;self.spans={}
    def _now(self):
        if not self.available: return None
        try:
            now=self.clock()
            if type(now) not in (int,float) or not math.isfinite(now) or (self.last is not None and now<self.last):
                raise ValueError('Invalid diagnostic clock')
            if self.origin is None:self.origin=now
            self.last=now;return now-self.origin
        except Exception:
            self.available=False;return None
    @contextmanager
    def measure(self,name):
        with self.lock:
            if name not in SPAN_NAMES or name in self.spans:raise ValueError('Duplicate/unknown diagnostic span')
            self.spans[name]={'start':self._now(),'end':None}
        try:yield
        finally:
            with self.lock:self.spans[name]['end']=self._now()
    def snapshot(self):
        with self.lock:
            return {'schema':'cityverse.batch-spans/v1','clockAvailable':self.available,
                    'spans':{k:dict(v) for k,v in self.spans.items()}}
COMMISSIONING_PROFILE = 'audio-1mbps-embb-bidir10s-v2'
SHARD_PROFILE = 'round-500-bidir18s-v4'
SHARD_DURATION_SECONDS = 18
SHARD_EVIDENCE_MAX_AGE_SECONDS = 25
SHARD_POST_CLEANUP_MAX_AGE_SECONDS = 35
SHARD_LAUNCH_INTERVAL_MS = 35
SHARD_LAUNCH_SLOTS = 20
SHARD_SERVER_READINESS_ATTEMPTS = 100
SHARD_RELEASE_LEAD_SECONDS = 7
SHARD_ADMISSION_TIMEOUT_SECONDS = 47


def _profile(profile):
    if profile not in (LEGACY_PROFILE, COMMISSIONING_PROFILE, SHARD_PROFILE):
        raise PolicyBlocked('Unknown batch profile')
    return profile == COMMISSIONING_PROFILE
BATCH_ERROR_CODES = frozenset('batch.' + name for name in (
    'receiver_socket_identity_mismatch', 'client_socket_identity_mismatch', 'iperf_cookie_mismatch',
    'worker_failed', 'worker_result_invalid', 'worker_timestamp_invalid', 'receiver_timestamp_invalid',
    'native_binding_changed', 'cleanup_unverified', 'window_unverified', 'offered_load_mismatch',
    'receiver_readiness_failed', 'failure_persistence_failed', 'transport_deadline_expired',
    'transport_command_invalid', 'detached_control_failed'))


class BatchBlocked(PolicyBlocked):
    def __init__(self, code, message):
        if code not in BATCH_ERROR_CODES:
            raise ValueError('Unrecognized local batch failure category')
        self.code = code
        super().__init__(message)


def release_budget_failure(reason, message, *, now, child_deadline, transport_deadline, required):
    """Attach bounded numeric diagnostics without changing a scheduling gate."""
    failure = BatchBlocked('batch.receiver_readiness_failed', message)
    failure.release_budget_diagnostic = {
        'kind': 'release_budget', 'reasonCode': reason, 'observerMonotonic': now,
        'childDeadline': child_deadline, 'transportDeadline': transport_deadline,
        'requiredSeconds': required, 'remainingSeconds': max(0., child_deadline - now)}
    return failure


def release_schedule(*, wall, mono_before, mono_after, child_deadline, transport_deadline, duration):
    """One common epoch inside the ORIGINAL child/transport lifetimes.

    Bracket the wall read with monotonic samples. Earliest mapping fences
    release completion two seconds before epoch; latest mapping admits the
    actual measurement plus two seconds tail before the same child deadline.
    No fallback epoch, timeout growth, or replay after partial release.
    """
    values=(wall,mono_before,mono_after,child_deadline,transport_deadline)
    if (any(type(v) not in (int,float) or not math.isfinite(v) for v in values)
            or not 0<=mono_before<=mono_after or wall<0
            or type(duration) is not int or duration not in (5, 10, SHARD_DURATION_SECONDS)):
        raise BatchBlocked('batch.receiver_readiness_failed','Invalid release scheduling clocks or duration')
    epoch=math.ceil(wall)+(SHARD_RELEASE_LEAD_SECONDS if duration == SHARD_DURATION_SECONDS else 6)
    lead=epoch-wall
    if mono_after+lead+duration+2>child_deadline:
        raise release_budget_failure('insufficient_child_completion', 'Insufficient original child completion budget',
            now=mono_after, child_deadline=child_deadline, transport_deadline=transport_deadline,
            required=lead+duration+2)
    deadline=min(transport_deadline,mono_before+lead-2)
    if deadline<=mono_after:
        raise release_budget_failure('no_release_dispatch_budget', 'No original release dispatch budget remains',
            now=mono_after, child_deadline=child_deadline, transport_deadline=transport_deadline,
            required=lead+duration+2)
    return epoch,deadline


class BatchReleaseBarrier:
    """Give every shard one release epoch after all shard clients are ready."""
    def __init__(self, participants, *, clock=time.time, monotonic=time.monotonic,
                 cancelled=lambda: False):
        if (type(participants) is not int or not 2 <= participants <= 10
                or not all(callable(value) for value in (clock, monotonic, cancelled))):
            raise PolicyBlocked('Invalid batch release barrier')
        self.participants = participants
        self.clock, self.monotonic, self.cancelled = clock, monotonic, cancelled
        self.condition = Condition()
        self.arrivals = []
        self.result = None
        self.error = None

    def abort(self):
        with self.condition:
            if self.result is None and self.error is None:
                self.error = BatchBlocked('batch.receiver_readiness_failed',
                                          'Shard readiness failed before common release')
            self.condition.notify_all()

    def schedule(self, *, wall, mono_before, mono_after, child_deadline,
                 transport_deadline, duration):
        # Validate each clock mapping before it can influence the shared epoch.
        release_schedule(wall=wall, mono_before=mono_before, mono_after=mono_after,
                         child_deadline=child_deadline, transport_deadline=transport_deadline,
                         duration=duration)
        with self.condition:
            if self.result is not None or self.error is not None or len(self.arrivals) >= self.participants:
                raise BatchBlocked('batch.receiver_readiness_failed', 'Invalid shard release barrier state')
            self.arrivals.append((wall, mono_before, mono_after, child_deadline, transport_deadline, duration))
            if len(self.arrivals) == self.participants:
                try:
                    if len({row[5] for row in self.arrivals}) != 1:
                        raise BatchBlocked('batch.receiver_readiness_failed', 'Shard durations differ at release')
                    before = self.monotonic()
                    shared_wall = self.clock()
                    after = self.monotonic()
                    self.result = release_schedule(
                        wall=shared_wall, mono_before=before, mono_after=after,
                        child_deadline=min(row[3] for row in self.arrivals),
                        transport_deadline=min(row[4] for row in self.arrivals), duration=duration)
                except BaseException as exc:
                    self.error = exc
                self.condition.notify_all()
            while self.result is None and self.error is None:
                if self.cancelled():
                    self.error = BatchBlocked('batch.receiver_readiness_failed',
                                              'Common shard release was cancelled')
                    self.condition.notify_all()
                    break
                now = self.monotonic()
                remaining = min(row[3] for row in self.arrivals) - duration - 2 - now
                if remaining <= 0:
                    self.error = release_budget_failure('common_release_deadline_expired',
                        'Common shard release deadline expired', now=now,
                        child_deadline=min(row[3] for row in self.arrivals),
                        transport_deadline=min(row[4] for row in self.arrivals), required=duration+2)
                    self.condition.notify_all()
                    break
                self.condition.wait(timeout=min(.05, remaining))
            if self.error is not None:
                raise self.error
            return self.result


def readiness_seconds(groups):
    """Bound 50+ flow startup allowance without renewing any child lifetime."""
    largest = max((len(group) for group in groups.values()), default=0)
    if not 1 <= largest <= 100:
        raise BatchBlocked('batch.receiver_readiness_failed', 'Invalid readiness group size')
    return min(10.0, 8.0 + max(0, largest - 50) * 0.04)


def receiver_readiness_seconds(groups, profile):
    """Allocate shared-N6 shard startup slack inside the fixed prep envelope."""
    base = readiness_seconds(groups)
    return min(10.0, base + 2.0) if profile == SHARD_PROFILE else base


@dataclass(frozen=True)
class BatchJob:
    key: str
    supi: str
    generation: int
    ue_pod: str
    ue_uid: str
    receiver_pod: str
    receiver_uid: str
    ue_ip: str
    tun: str
    endpoint: str
    receiver_peer: str
    direction: str
    port: int
    offered_kbps: int
    session_id: str
    pfcp_seid: str
    sm_context_ref: str
    dnn: str
    sst: int
    sd: str
    upf_name: str
    upf_epoch: str
    duration: int = 5
    route_binding_id: str = ""
    route_version: str = ""
    route_path_revision: str = ""


@dataclass(frozen=True)
class DetachedReceiverContract:
    """Canonical reservation provenance, never a caller-selected time limit."""
    canonical: str
    sha256: str


def detached_receiver_contract(reservation, jobs, *, round_id, generation,
                               shard_index, shard_count, token):
    # Lazy import avoids the provider/worker module import cycle. Use the same
    # complete reservation validation as dispatch, including fixed old offsets.
    from backend.onprem.traffic.batch import validate_detached_reservation
    if (type(round_id) is not str or re.fullmatch(_UUID, round_id) is None
            or type(generation) is not int or generation < 1
            or type(shard_index) is not int or type(shard_count) is not int
            or not 2 <= shard_count <= 10 or not 0 <= shard_index < shard_count
            or type(token) is not str or re.fullmatch(r'[a-f0-9]{32}', token) is None
            or not isinstance(reservation, dict)
            or any(type(reservation.get(key)) is not int for key in ('generation', 'shardIndex', 'shardCount'))
            or not isinstance(jobs, (tuple, list)) or not jobs
            or any(type(job) is not BatchJob for job in jobs)
            or any(type(job.generation) is not int or job.generation != generation for job in jobs)
            or not validate_detached_reservation(reservation, round_id=round_id,
                generation=generation, shard_index=shard_index, shard_count=shard_count,
                token=token, jobs=jobs)):
        raise PolicyBlocked('Invalid detached receiver reservation binding')
    value = dict(schema='cityverse.detached-receiver-contract/v1', roundId=round_id,
                 generation=generation, shardIndex=shard_index, shardCount=shard_count,
                 token=token, jobs=[asdict(job) for job in jobs], reservation=reservation)
    try:
        canonical = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise PolicyBlocked('Invalid detached receiver contract encoding') from None
    return DetachedReceiverContract(canonical, hashlib.sha256(canonical.encode()).hexdigest())


def validate_receiver_contract(contract, jobs, **expected):
    """Revalidate frozen bytes, full jobs, reservation and optional caller scope."""
    if (type(contract) is not DetachedReceiverContract or type(contract.canonical) is not str
            or type(contract.sha256) is not str
            or hashlib.sha256(contract.canonical.encode()).hexdigest() != contract.sha256):
        raise PolicyBlocked('Invalid detached receiver contract digest')
    try:
        value = json.loads(contract.canonical)
        if not isinstance(value, dict) or value.get('schema') != 'cityverse.detached-receiver-contract/v1':
            raise ValueError()
        fields = dict(round_id=value['roundId'], generation=value['generation'],
                      shard_index=value['shardIndex'], shard_count=value['shardCount'], token=value['token'])
        if any(key not in fields or type(fields[key]) is not type(item) or fields[key] != item
               for key, item in expected.items()):
            raise ValueError()
        rebuilt = detached_receiver_contract(value['reservation'], jobs, **fields)
        if rebuilt != contract:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise PolicyBlocked('Invalid detached receiver contract binding') from None
    return value['reservation']


def _text(value, pattern, label):
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise PolicyBlocked(f"Invalid batch {label}")
    return value


def build_jobs(bindings, *, aggregate_kbps=10_000, offered_by_supi=None, profile=LEGACY_PROFILE,
               shard_index=0, traffic_mode="functional"):
    """Compile immutable, shell-safe jobs; no resource/Pod ownership inference.

    Caller must supply exact observed resource identities, then fence them
    before every remote operation. No caller-supplied commands are accepted.
    """
    v2 = _profile(profile)
    if traffic_mode not in ("functional", "qos"):
        raise PolicyBlocked("Unknown traffic mode")
    qos = traffic_mode == "qos"
    if qos and (v2 or offered_by_supi is None):
        raise PolicyBlocked("QoS requires explicit rates and cannot use commissioning profiles")
    if type(shard_index) is not int or not 0 <= shard_index < 10 or (profile != SHARD_PROFILE and shard_index):
        raise PolicyBlocked('Invalid batch shard index')
    if v2 and (not isinstance(bindings,(list,tuple)) or len(bindings)!=2
            or aggregate_kbps!=8000 or offered_by_supi is not None
            or any(not isinstance(b,dict) or b.get('slice')!='embb' for b in bindings)):
        raise PolicyBlocked('Commissioning requires ordered target/control eMBB bindings and exact 8000kbps budget')
    if not isinstance(bindings, (list, tuple)) or not 1 <= len(bindings) <= 50:
        raise PolicyBlocked("Batch requires 1 to 50 actual UE bindings")
    if any(not isinstance(b, dict) for b in bindings):
        raise PolicyBlocked('Each batch binding must be an observed object')
    if type(aggregate_kbps) is not int or not len(bindings) <= aggregate_kbps <= (400_000 if qos else 10_000):
        raise PolicyBlocked("Aggregate offered load must be bounded integer kbps")
    jobs, identities, addresses = [], set(), set()
    generations = set()
    pod_uids, receiver_by_slice, interfaces, smf_refs, pfcp_ids = {}, {}, set(), set(), set()
    rate = aggregate_kbps // len(bindings)
    if offered_by_supi is not None:
        if (not isinstance(offered_by_supi, dict) or
                set(offered_by_supi) != {b.get('supi') for b in bindings} or
                any(not valid_rate(v) or (not qos and type(v) is not int) for v in offered_by_supi.values()) or
                rate_budget(offered_by_supi) > aggregate_kbps):
            raise PolicyBlocked('Per-UE offered load must fit the explicit aggregate budget')
        if qos and any(direction_rate(v, d) > 4000 for v in offered_by_supi.values() for d in ('ul', 'dl')):
            raise PolicyBlocked('QoS per-UE load exceeds scenario limit')
    for index, binding in enumerate(bindings):
        try:
            slice_name = binding['slice']
            if slice_name not in _POOLS:
                raise PolicyBlocked("Unknown batch slice")
            ip = str(ipaddress.IPv4Address(binding['ip']))
            if ipaddress.ip_address(ip) not in _POOLS[slice_name]:
                raise PolicyBlocked("UE address does not belong to selected slice")
            supi = _text(binding['supi'], r'imsi-[0-9]{15}', 'SUPI')
            uid = _text(binding['podUid'], r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', 'UE UID')
            receiver_uid = _text(binding['receiverUid'], r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', 'receiver UID')
            pod = _text(binding['pod'], r'cv-ue-g[0-9]+-[0-9]+', 'UE Pod')
            route = binding.get('serviceRoute')
            receiver = _text(binding['receiverPod'], r'[a-z0-9][a-z0-9.-]{0,252}' if route else rf'n6-{slice_name}-[a-z0-9]+-[a-z0-9]+', 'receiver Pod')
            tun = _text(binding['tun'], r'(?:uesimtun|ueTun)[0-9]+', 'TUN')
            generation = binding['generation']
            session_id = _text(binding['sessionId'], r'[1-9][0-9]*', 'NAS PDU session ID')
            pfcp_seid = _text(binding['pfcpSeid'], r'[1-9][0-9]*', 'native PFCP SEID')
            if int(session_id) > 255 or int(pfcp_seid) > 2**64-1:
                raise PolicyBlocked('NAS/PFCP session identity is out of range')
            sm_context_ref = _text(binding['smContextRef'], 'urn:uuid:' + _UUID, 'full SMF context ref')
            # NativeUPFObserver retains the actual /proc/self/ns/net readlink,
            # e.g. net:[4026532325], not a guessed bare inode number.
            upf_epoch = _text(binding['upfEpoch'], _UUID + r':net:\[[1-9][0-9]*\]:[1-9][0-9]*:' + _UUID, 'native UPF epoch')
            if sm_context_ref in smf_refs or (upf_epoch, pfcp_seid) in pfcp_ids:
                raise PolicyBlocked('Duplicate full SMF context or native PFCP session')
            smf_refs.add(sm_context_ref)
            pfcp_ids.add((upf_epoch, pfcp_seid))
            if (binding['dnn'] != 'internet' or type(binding['sst']) is not int
                    or (binding['sst'], binding['sd']) != _NSSAI[slice_name]
                    or (not route and binding['upf'] != 'upf-' + slice_name)):
                raise PolicyBlocked('Native DNN/S-NSSAI/UPF identity mismatch')
            if type(generation) is not int or generation < 1 or not pod.startswith(f'cv-ue-g{generation}-'):
                raise PolicyBlocked("Batch generation mismatch")
            if supi in identities or ip in addresses:
                raise PolicyBlocked("Duplicate batch UE identity or address")
            for name, observed_uid in ((pod, uid), (receiver, receiver_uid)):
                if name in pod_uids and pod_uids[name] != observed_uid:
                    raise PolicyBlocked('One Pod name cannot refer to multiple UID epochs')
                pod_uids[name] = observed_uid
            if not route and slice_name in receiver_by_slice and receiver_by_slice[slice_name] != (receiver, receiver_uid):
                raise PolicyBlocked('Each slice requires exactly one receiver identity')
            receiver_by_slice[slice_name] = (receiver, receiver_uid)
            if (pod, tun) in interfaces:
                raise PolicyBlocked('One UE TUN cannot belong to multiple batch identities')
            interfaces.add((pod, tun))
            identities.add(supi)
            addresses.add(ip)
            generations.add(generation)
            endpoint, peer = _ENDPOINTS[slice_name], _SNAT_PEERS[slice_name]
            if route:
                from backend.onprem.intents.service_routes import ServiceRouteBinding
                try:
                    ServiceRouteBinding(**{k: route[k] for k in ServiceRouteBinding.__dataclass_fields__})
                except Exception:
                    raise PolicyBlocked('Invalid commissioned service route') from None
                if (route['application_pod_uid'] != receiver_uid or route['receiver_pod'] != receiver
                        or route['upf_target_id'] != binding['upf'] or route['upf_epoch'] != upf_epoch
                        or str(route['pfcp_seid']) != pfcp_seid or route['generation'] != generation
                        or json.loads(route['session_key'])[1:] != [supi, int(session_id), sm_context_ref]
                        or not route['accepting'] or not route.get('path_revision')):
                    raise PolicyBlocked('Service route differs from exact native flow identity')
                endpoint, peer = route['address'], route['receiver_peer']
            ue_rate = rate if offered_by_supi is None else offered_by_supi[supi]
            for offset, direction in enumerate(('ul', 'dl')):
                jobs.append(BatchJob(f'u{index:02d}-{direction}', supi, generation, pod, uid,
                                     receiver, receiver_uid, ip, tun, endpoint, peer,
                                     direction, 5200 + shard_index * 100 + index * 2 + offset, direction_rate(ue_rate, direction),
                                     session_id, pfcp_seid, sm_context_ref, binding['dnn'], binding['sst'], binding['sd'],
                                     binding['upf'], upf_epoch, route_binding_id=route['binding_id'] if route else '',
                                     route_version=route['version'] if route else '', route_path_revision=route['path_revision'] if route else ''))
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyBlocked("Incomplete or malformed batch binding") from exc
    if len(generations) != 1:
        raise PolicyBlocked("Mixed batch generations are forbidden")
    if v2:
        ports={'u00-ul':5201,'u00-dl':5203,'u01-ul':5202,'u01-dl':5204}
        jobs=[replace(j,port=ports[j.key],duration=10) for j in jobs]
    elif profile == SHARD_PROFILE:
        jobs=[replace(j,duration=SHARD_DURATION_SECONDS) for j in jobs]
    return tuple(jobs)


def build_shards(bindings, *, round_id, generation, aggregate_kbps, offered_by_supi, traffic_mode="functional", minimum_shards=1):
    """Compile exact 1..500 round bindings into bounded, port-disjoint shards."""
    _text(round_id, _UUID, 'round ID')
    if traffic_mode not in ('functional', 'qos'):
        raise PolicyBlocked('Unknown traffic mode')
    qos = traffic_mode == 'qos'
    if (not isinstance(bindings, (list, tuple)) or not 1 <= len(bindings) <= (100 if qos else 500)
            or type(generation) is not int or generation < 1
            or any(not isinstance(b, dict) or b.get('generation') != generation for b in bindings)
            or type(aggregate_kbps) is not int or not len(bindings) <= aggregate_kbps <= (400_000 if qos else 10_000)
            or not isinstance(offered_by_supi, dict)
            or set(offered_by_supi) != {b.get('supi') for b in bindings}
            or len(offered_by_supi) != len(bindings)
            or any(not valid_rate(v) or (not qos and type(v) is not int) for v in offered_by_supi.values())
            or rate_budget(offered_by_supi) > aggregate_kbps):
        raise PolicyBlocked('Invalid exact round batch population or budget')
    # Use every available shard once the population exceeds ten, and stripe
    # each immutable UE-Pod population across those shards.  Common release
    # synchronizes permission to start, but cannot bound scheduler/handshake
    # delay inside one 100-process client worker.  With the platform's fixed
    # 50-UE Pod capacity this keeps each 500-UE client worker at five UEs / ten
    # flows while retaining the ten-shard global barrier and port partition.
    from backend.onprem.traffic.planning import shard_supis
    try:
        partition = shard_supis(bindings, minimum_shards=minimum_shards)
    except ValueError as exc:
        raise PolicyBlocked(str(exc)) from None
    by_supi = {row['supi']: row for row in bindings}
    buckets = [[by_supi[supi] for supi in sorted(group)] for group in partition]
    shards = []
    for index, rows in enumerate(buckets):
        rates = {b['supi']: offered_by_supi[b['supi']] for b in rows}
        shards.append(build_jobs(rows, aggregate_kbps=rate_budget(rates), offered_by_supi=rates,
                                 profile=SHARD_PROFILE, shard_index=index, traffic_mode=traffic_mode))
    jobs = [job for shard in shards for job in shard]
    if (len(jobs) != 2 * len(bindings) or len({(j.supi, j.direction) for j in jobs}) != len(jobs)
            or len({j.port for j in jobs}) != len(jobs)
            or len({j.ue_ip for j in jobs}) != len(bindings)
            or len({(j.ue_pod, j.tun) for j in jobs}) != len(bindings)
            or len({j.sm_context_ref for j in jobs}) != len(bindings)
            or len({(j.upf_epoch, j.pfcp_seid) for j in jobs}) != len(bindings)
            or any(len({j.ue_uid for j in jobs if j.ue_pod == pod}) != 1
                   for pod in {j.ue_pod for j in jobs})
            or any(len({(j.receiver_pod, j.receiver_uid) for j in jobs if j.upf_name == upf}) != 1
                   for upf in {j.upf_name for j in jobs})):
        raise PolicyBlocked('Duplicate cross-shard round identity')
    return tuple(shards)


def validate_socket_pair(job, client, server):
    """Validate the two ends of one NAT-traversing flow, never infer its peer."""
    if not isinstance(client, dict) or not isinstance(server, dict):
        raise BatchBlocked('batch.worker_result_invalid', 'Non-object actual iperf report')
    connected = client.get('start', {}).get('connected', [])
    test = client.get('start', {}).get('test_start', {})
    peer = server.get('start', {}).get('connected', [])
    if (len(connected) != 1 or connected[0].get('local_host') != job.ue_ip
            or connected[0].get('remote_host') != job.endpoint
            or connected[0].get('remote_port') != job.port
            or test.get('protocol') != 'UDP' or test.get('reverse') != int(job.direction == 'dl')):
        raise BatchBlocked('batch.client_socket_identity_mismatch', 'Actual traffic binding or direction mismatch')
    if (len(peer) != 1 or peer[0].get('local_host') != job.endpoint
            or peer[0].get('remote_host') != job.receiver_peer or peer[0].get('local_port') != job.port):
        raise BatchBlocked('batch.receiver_socket_identity_mismatch', 'Actual receiver socket identity mismatch')
    cookie = client.get('start', {}).get('cookie')
    if (not isinstance(cookie, str) or re.fullmatch(r'[A-Za-z0-9]{36}', cookie) is None
            or server.get('start', {}).get('cookie') != cookie):
        raise BatchBlocked('batch.iperf_cookie_mismatch', 'Client and receiver do not identify the same iperf session')


def _accounting_clock_present(report):
    return isinstance(report, dict) and isinstance(report.get('start'), dict) and 'accounting_timestamp' in report['start']


def detached_accounting_interval(job, client, server, client_start, client_finish,
                                 server_start, server_finish, *, reservation=None):
    """Conservative overlap of local iperf accounting phases, not packet timing.

    iperf 3.21 records its JSON timestamp on connection, before test init.
    A local phase of duration d therefore starts in [L, F-d], where L is
    connection time and F its own wrapper finish. Its guaranteed interval is
    [F-d, L+d]. Explicit local summaries avoid legacy UDP sum's peer duration.
    Instrumented v2 reports instead return a bracketed CLOCK_BOOTTIME interval;
    their caller must validate the same boot and supply its observation clock.
    """
    starts, finishes, clock_modes = [], [], set()
    for report, begin, end, sending in ((client, client_start, client_finish, job.direction == 'ul'),
                                       (server, server_start, server_finish, job.direction == 'dl')):
        has_clock = _accounting_clock_present(report)
        phase_clock = report['start']['accounting_timestamp'] if has_clock else None
        clock_modes.add(has_clock)
        if (any(type(t) not in (int, float) or not math.isfinite(t) for t in (begin, end))
                or not has_clock and begin >= end or not isinstance(report, dict) or report.get('error')):
            raise BatchBlocked('batch.worker_timestamp_invalid', 'Invalid detached local wrapper')
        header = report.get('start')
        if not isinstance(header, dict):
            raise BatchBlocked('batch.worker_timestamp_invalid', 'Missing detached local report start')
        setup = header.get('test_start')
        if not isinstance(setup, dict):
            raise BatchBlocked('batch.window_unverified', 'Missing detached local accounting configuration')
        if (setup.get('protocol') != 'UDP' or type(setup.get('num_streams')) is not int
                or setup['num_streams'] != 1 or type(setup.get('omit')) is not int or setup['omit'] != 0
                or type(setup.get('reverse')) is not int or setup['reverse'] != int(job.direction == 'dl')
                or type(setup.get('bidir')) is not int or setup['bidir'] != 0):
            raise BatchBlocked('batch.window_unverified', 'Detached local accounting configuration differs')
        timestamp = header.get('timestamp')
        if not isinstance(timestamp, dict):
            raise BatchBlocked('batch.worker_timestamp_invalid', 'Missing detached local connection clock')
        seconds, millis = timestamp.get('timesecs'), timestamp.get('timemillisecs')
        if (type(seconds) is not int or type(millis) is not int or millis < 0
                or millis // 1000 != seconds
                or not has_clock and (millis / 1000 > end
                    or math.nextafter((millis + 1) / 1000, math.inf) < begin)):
            raise BatchBlocked('batch.worker_timestamp_invalid', 'Detached connection clock is outside its own wrapper')
        totals = report.get('end')
        summary = totals.get('sum_sent' if sending else 'sum_received') if isinstance(totals, dict) else None
        if not isinstance(summary, dict) or summary.get('sender') is not sending:
            raise BatchBlocked('batch.window_unverified', 'Missing explicit local accounting duration')
        duration = summary.get('seconds')
        if (type(duration) not in (int, float) or not math.isfinite(duration) or not 17 <= duration <= 20
                or type(summary.get('start')) not in (int, float) or summary['start'] != 0
                or type(summary.get('end')) not in (int, float) or summary['end'] != duration):
            raise BatchBlocked('batch.window_unverified', 'Invalid explicit local accounting duration')
        # iperf_time.c truncates each clock endpoint to microseconds before
        # subtraction, so elapsed may overstate the phase by <1us. Bash v2
        # EPOCHREALTIME likewise has six fractional digits. Shrink, never
        # expand, the guaranteed interval; nextafter covers float rounding.
        lower = math.nextafter(millis / 1000, -math.inf)
        upper = math.nextafter(end + .000001, math.inf)
        duration_lower = math.nextafter(math.nextafter(duration, 0) - .000001, 0)
        if has_clock:
            # The pinned instrumented binary brackets the SAME monotonic
            # start_time used by this local summary with boot-clock reads.
            # A connection timestamp and a delayed wrapper reap are not the
            # accounting origin. Unknown/malformed phase clocks fail closed.
            if (not isinstance(phase_clock, dict)
                    or set(phase_clock) != {'schema', 'clock', 'before_usec', 'after_usec'}
                    or phase_clock.get('schema') != 'cityverse.iperf-accounting/v2'
                    or phase_clock.get('clock') != 'CLOCK_BOOTTIME'):
                raise BatchBlocked('batch.worker_timestamp_invalid', 'Invalid local accounting clock schema')
            before, after = phase_clock['before_usec'], phase_clock['after_usec']
            if (any(type(t) not in (int, float) or not math.isfinite(t) or t < 0
                    or t > 2**53 or int(t) != t for t in (before, after))
                    or before > after):
                raise BatchBlocked('batch.worker_timestamp_invalid', 'Invalid local accounting clock bracket')
            lower = math.nextafter(before / 1_000_000, -math.inf)
            upper = math.nextafter((after + 1) / 1_000_000, math.inf)
            if reservation is not None:
                deadline = reservation['clientChildDeadline' if report is client else 'serverChildDeadline']
                if (before / 1_000_000 < reservation['createdMonotonic']
                        or (after + 1) / 1_000_000 + duration > deadline):
                    raise BatchBlocked('batch.worker_timestamp_invalid', 'Local accounting is outside its immutable boot deadline')
            starts.append(math.nextafter(upper, math.inf))
            finishes.append(math.nextafter(lower + duration_lower, -math.inf))
            continue
        if lower > upper - duration_lower:
            raise BatchBlocked('batch.window_unverified', 'Detached local accounting exceeds its wrapper envelope')
        starts.append(math.nextafter(max(begin, upper - duration_lower), math.inf))
        finishes.append(math.nextafter(min(end, lower + duration_lower), -math.inf))
    if len(clock_modes) != 1:
        raise BatchBlocked('batch.worker_timestamp_invalid', 'Mixed local accounting clock domains')
    return max(starts), min(finishes)


def reduce_evidence(jobs, records, *, now, maximum_age=15, profile=LEGACY_PROFILE,
                    receiver_contract=None, observer_boottime=None):
    """Verify every actual client+receiver record, never fill missing samples.

    Records contain worker wall-clock start/end timestamps, not host exec
    launch times. Caller additionally needs native and resource ownership
    guards before/after execution; this reducer alone is not a live gate.
    """
    if receiver_contract is not None and profile != SHARD_PROFILE:
        raise PolicyBlocked('Detached receiver contract requires the shard profile')
    if _profile(profile):
        from archive.legacy.scenarios.commissioning import reduce_baseline
        if maximum_age!=15:raise PolicyBlocked('Fixed commissioning freshness required')
        return reduce_baseline(jobs,records,now=now)
    sharded = profile == SHARD_PROFILE
    duration = SHARD_DURATION_SECONDS if sharded else 5
    # The fixed shard host lifetime leaves a bounded wrapper tail for
    # collecting the actual iperf JSON.  This bound
    # never extends the measured traffic interval or the 25-second age gate.
    worker_window_max = 46 if sharded else 32
    # A receiver is armed before both readiness phases and the common release.
    # Its successful wrapper lifetime is independently capped by the generated
    # server child timeout; do not reject that admitted tail using the shorter
    # client collection bound.
    receiver_window_max = SHARD_SERVER_CHILD_TIMEOUT_SECONDS if sharded else 32
    reservation = None
    if receiver_contract is not None:
        if not sharded:
            raise PolicyBlocked('Detached receiver contract requires the shard profile')
        reservation = validate_receiver_contract(receiver_contract, jobs)
        receiver_window_max = reservation['serverChildDeadline'] - reservation['createdMonotonic']
    if any(j.duration != duration for j in jobs):
        raise PolicyBlocked('Batch job duration does not match its profile')
    if not jobs or len(jobs) > 100 or len({j.key for j in jobs}) != len(jobs):
        raise PolicyBlocked("Invalid batch job set")
    if set(records) != {j.key for j in jobs}:
        raise PolicyBlocked("Missing or extra actual batch measurements")
    if type(now) not in (int, float) or not math.isfinite(now):
        raise PolicyBlocked("Invalid observation clock")
    use_phase_clock = False
    if receiver_contract is not None:
        modes = {(_accounting_clock_present(row.get('client')), _accounting_clock_present(row.get('server')))
                 for row in records.values() if isinstance(row, dict)}
        if len(modes) != 1 or next(iter(modes)) not in {(False, False), (True, True)}:
            raise BatchBlocked('batch.worker_timestamp_invalid', 'Mixed batch accounting clock domains')
        use_phase_clock = next(iter(modes)) == (True, True)
        if use_phase_clock and (type(observer_boottime) not in (int, float)
                or not math.isfinite(observer_boottime) or observer_boottime < reservation['createdMonotonic']):
            raise BatchBlocked('batch.worker_timestamp_invalid', 'Missing coherent collector boot clock')
    samples, starts, finishes = [], [], []
    for job in jobs:
        record = records[job.key]
        if (record.get('job') != job or type(record.get('clientExit')) is not int
                or type(record.get('serverExit')) is not int
                or record['clientExit'] != 0 or record['serverExit'] != 0):
            raise BatchBlocked('batch.worker_failed', "Batch worker failed or identity changed")
        start, finish = record.get('startedAt'), record.get('finishedAt')
        if any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, finish)):
            raise BatchBlocked('batch.worker_timestamp_invalid', "Missing actual worker timestamps")
        # The wrapper may remain runnable after iperf has completed while a
        # CPU-throttled Pod serializes JSON collection and process reaping.
        # Bound that lifecycle by the worker timeout, but never count it as
        # traffic.  The common window below is derived from iperf's own start
        # timestamp and measured sender/receiver durations.
        if not use_phase_clock and (not duration-1 <= finish-start <= worker_window_max
                or not 0 <= now-finish < maximum_age or start > now):
            raise BatchBlocked('batch.window_unverified', "Incomplete, future or stale batch window")
        receiver_start, receiver_finish = record.get('receiverStartedAt'), record.get('receiverFinishedAt')
        if any(type(t) not in (int, float) or not math.isfinite(t) for t in (receiver_start, receiver_finish)):
            raise BatchBlocked('batch.receiver_timestamp_invalid', 'Missing actual receiver worker timestamps')
        if not use_phase_clock and (not duration-1 <= receiver_finish-receiver_start <= receiver_window_max
                or not 0 <= now-receiver_finish < maximum_age
                or receiver_start > start + 1
                or receiver_contract is None and (receiver_finish < finish - 1 or receiver_finish > finish + 3)):
            raise BatchBlocked('batch.receiver_timestamp_invalid', 'Receiver window is stale or does not cover client measurement')
        # Detached receipts prove each local accounting interval against its
        # own wrapper below. Process reaping/JSON tails need not finish together;
        # their difference is not evidence that UDP measurement was truncated.
        client, server = record['client'], record['server']
        if not isinstance(client, dict) or not isinstance(server, dict):
            raise PolicyBlocked('Non-object actual iperf report')
        if client.get('error') or server.get('error'):
            raise PolicyBlocked("Actual iperf process reported failure")
        validate_socket_pair(job, client, server)
        timestamp = client.get('start', {}).get('timestamp')
        if not isinstance(timestamp, dict):
            raise BatchBlocked('batch.worker_timestamp_invalid', 'Missing actual iperf start timestamp')
        seconds, milliseconds = timestamp.get('timesecs'), timestamp.get('timemillisecs')
        if (type(seconds) is not int or type(milliseconds) is not int or milliseconds < 0
                or milliseconds // 1000 != seconds):
            raise BatchBlocked('batch.worker_timestamp_invalid', 'Invalid actual iperf start timestamp')
        iperf_start = milliseconds / 1000
        # The wrapper records immediately before exec.  iperf's timestamp must
        # therefore be inside that exact process lifetime.  This rejects mixed
        # clocks and prevents an unrelated report from supplying the window.
        if receiver_contract is None and not start <= iperf_start <= finish:
            raise BatchBlocked('batch.worker_timestamp_invalid', 'Actual iperf start is outside its worker lifetime')
        received = receiver_summary(server if job.direction == 'ul' else client)
        sent = sender_summary(client if job.direction == 'ul' else server)
        if not duration-1 <= received['seconds'] <= duration+2 or not duration-1 <= sent['seconds'] <= duration+2:
            raise BatchBlocked('batch.window_unverified', "Truncated actual measurement duration")
        if not 0.8 * job.offered_kbps * 1000 <= sent['bits_per_second'] <= 1.2 * job.offered_kbps * 1000:
            raise BatchBlocked('batch.offered_load_mismatch', "Actual offered workload differs from calibration")
        effective_start = max(iperf_start, receiver_start)
        # Both summaries describe the same actual UDP interval.  Taking the
        # shorter duration is conservative for asymmetric report tails and
        # prevents wrapper wall time from manufacturing overlap.
        effective_finish = min(finish, receiver_finish,
                               iperf_start + sent['seconds'], iperf_start + received['seconds'])
        if receiver_contract is not None:
            effective_start, effective_finish = detached_accounting_interval(
                job, client, server, start, finish, receiver_start, receiver_finish, reservation=reservation)
        if effective_finish <= effective_start:
            raise BatchBlocked('batch.window_unverified', 'Actual iperf interval is empty')
        starts.append(effective_start)
        finishes.append(effective_finish)
        display_start = now - (observer_boottime - effective_start) if use_phase_clock else effective_start
        display_finish = now - (observer_boottime - effective_finish) if use_phase_clock else effective_finish
        samples.append({'job': job, 'startedAt': display_start if use_phase_clock else start,
                        'finishedAt': display_finish if use_phase_clock else min(finish, receiver_finish),
                        'effectiveStartedAt': display_start,
                        'effectiveFinishedAt': display_finish,
                        'iperfStartedAt': iperf_start,
                        'receiverStartedAt': receiver_start, 'receiverFinishedAt': receiver_finish,
                        'receivedMbps': received['bits_per_second'] / 1_000_000,
                        'offeredObservedMbps': sent['bits_per_second'] / 1_000_000,
                        'packets': received['packets'], 'lostPackets': received['lost_packets'],
                        'client': client, 'server': server})
        if receiver_contract is not None:
            samples[-1]['receiverContractDigest'] = receiver_contract.sha256
            samples[-1]['clientFinishedAt'] = finish
    observer = observer_boottime if use_phase_clock else now
    if (min(finishes) - max(starts) < (8 if sharded else 4)
            or not 0 <= observer - min(finishes) < maximum_age):
        raise BatchBlocked('batch.window_unverified', "Batch windows did not overlap or remain jointly fresh")
    return tuple(samples)


class AccountingObservation:
    """Persist the exact clock pair used to project kernel accounting into UTC."""

    SCHEMA = 'cityverse.accounting-observation/v1'
    FIELDS = frozenset(('schemaVersion', 'clockSource', 'clockBootId', 'observerNow', 'observerBoottime'))

    @classmethod
    def valid(cls, value, *, boot_id=None):
        return (isinstance(value, dict) and set(value) == cls.FIELDS
            and value.get('schemaVersion') == cls.SCHEMA and value.get('clockSource') == 'CLOCK_BOOTTIME'
            and isinstance(value.get('clockBootId'), str) and re.fullmatch(_UUID, value['clockBootId']) is not None
            and (boot_id is None or value['clockBootId'] == boot_id)
            and all(type(value.get(k)) in (int, float) and math.isfinite(value[k]) and value[k] >= 0
                    for k in ('observerNow', 'observerBoottime')))


def aggregate_shards(shards, evidence, *, round_id, generation, now,
                     maximum_age=SHARD_POST_CLEANUP_MAX_AGE_SECONDS, execution_mode='attached',
                     observer_boottime=None):
    """Accept only a complete, fresh, simultaneous union of actual flow receipts.

    A receipt is {roundId, generation, shardIndex, token, jobs, records}.
    The caller must durably journal each shard before dispatch and preserve
    failed receipts separately; this function never converts partial data to a
    successful measurement.
    """
    _text(round_id, _UUID, 'round ID')
    if execution_mode not in {'attached', 'detached-shard-v1'}:
        raise PolicyBlocked('Invalid shard execution contract mode')
    if (type(generation) is not int or generation < 1 or not isinstance(shards, tuple)
            or not 1 <= len(shards) <= 10 or not isinstance(evidence, (list, tuple))
            or len(evidence) != len(shards)):
        raise PolicyBlocked('Incomplete round shard evidence')
    seen_tokens, samples, clock_modes, boot_ids = set(), [], set(), set()
    for index, expected in enumerate(shards):
        item = evidence[index]
        if (not isinstance(item, dict) or item.get('roundId') != round_id
                or item.get('generation') != generation or item.get('shardIndex') != index
                or item.get('jobs') != expected or not isinstance(item.get('records'), dict)):
            raise PolicyBlocked('Cross-round, missing or altered shard evidence')
        token = _text(item.get('token'), r'[a-f0-9]{32}', 'shard token')
        if token in seen_tokens:
            raise PolicyBlocked('Duplicate round shard token')
        seen_tokens.add(token)
        contract = item.get('receiverContract')
        if execution_mode == 'detached-shard-v1':
            reserved = validate_receiver_contract(contract, expected, round_id=round_id, generation=generation,
                                                 shard_index=index, shard_count=len(shards), token=token)
            boot_ids.add(reserved['clockBootId'])
        elif contract is not None or 'receiverContract' in item:
            raise PolicyBlocked('Attached shard cannot use a detached receiver contract')
        if contract is not None:
            clock_modes.update(_accounting_clock_present(row.get(role))
                for row in item['records'].values() if isinstance(row, dict) for role in ('client', 'server'))
            if len(clock_modes) > 1:
                raise BatchBlocked('batch.worker_timestamp_invalid', 'Mixed round accounting clock domains')
        samples.extend(reduce_evidence(expected, item['records'], now=now,
                                       maximum_age=maximum_age, profile=SHARD_PROFILE,
                                       receiver_contract=contract, observer_boottime=observer_boottime))
    flows = {(sample['job'].supi, sample['job'].direction) for sample in samples}
    expected_flows = {(job.supi, job.direction) for shard in shards for job in shard}
    if (len(flows) != len(samples) or flows != expected_flows
            or len({job.port for shard in shards for job in shard}) != len(samples)):
        raise PolicyBlocked('Duplicate or missing round flow')
    start = max(sample['effectiveStartedAt'] for sample in samples)
    finish = min(sample['effectiveFinishedAt'] for sample in samples)
    if finish - start < 8 or now - finish >= maximum_age:
        raise BatchBlocked('batch.window_unverified', 'All round flows lack an eight-second common effective window')
    result = {'samples': tuple(samples), 'effectiveStartedAt': start, 'effectiveFinishedAt': finish,
              'flowCount': len(samples), 'source': 'native-iperf3-receiver'}
    if clock_modes == {True}:
        if len(boot_ids) != 1:
            raise PolicyBlocked('Accounting shards do not share one kernel boot')
        observation = dict(schemaVersion=AccountingObservation.SCHEMA, clockSource='CLOCK_BOOTTIME',
            clockBootId=next(iter(boot_ids)), observerNow=now, observerBoottime=observer_boottime)
        if not AccountingObservation.valid(observation):
            raise PolicyBlocked('Invalid accounting observation clock pair')
        result['accountingObservation'] = observation
    return result


# Fixed program, never interpolated shell source. Values arrive as strictly
# validated argv/base64 TSV, and are only passed as quoted command arguments.
# Requirement B1: bounded process groups; B2: actual timestamps/JSON; B3: no
# broad process cleanup; B4: one scheduled batch exec per participating Pod.
WORKER_SCRIPT = r'''#!/usr/bin/env bash
set -Eeuo pipefail
usage() { printf '%s\n' 'Usage: worker server|client|ready|client_ready|release|cancel TOKEN EPOCH BASE64_TSV' >&2; }
[[ $# == 4 ]] || { usage; exit 2; }
role=$1 token=$2 epoch=$3 manifest=$4
[[ $token =~ ^[a-f0-9]{32}$ && $epoch =~ ^[0-9]+$ ]] || exit 2
[[ $role == server || $role == client || $role == ready || $role == client_ready || $role == release || $role == cancel || $role == status ]] || exit 2
directory=/tmp/cityverse-batch-$token
umask 077
start_time() { awk '{print $22}' "/proc/$1/stat" 2>/dev/null; }
live_process() {
  local pid=$1 started=$2 group=${3:-} value tail
  local -a fields
  IFS= read -r value < "/proc/$pid/stat" 2>/dev/null || return 1
  tail=${value##*) }; read -ra fields <<< "$tail"
  [[ ${fields[0]} != Z && ${fields[0]} != X && ${fields[19]} == "$started" &&
     ( -z $group || ${fields[2]} == "$group" ) ]]
}
group_absent() {
  local group=$1 stat_file value tail pgid
  for stat_file in /proc/[0-9]*/stat; do
    IFS= read -r value < "$stat_file" 2>/dev/null || continue
    tail=${value##*) }
    read -r _ _ pgid _ <<< "$tail"
    # Include zombies: an absent supervisor is not descendant-absence proof.
    [[ $pgid != "$group" ]] || return 1
  done
}
validate_audit_manifest() {
  local encoded=$1 decoded key endpoint port ip tun direction rate profile extra index
  decoded=$(printf '%s' "$encoded" | base64 --decode) || return 4
  audit_schema=; declare -gA audit_expected=() audit_flow_ports=() audit_ue_directions=()
  while IFS=$'\t' read -r key endpoint port ip tun direction rate profile extra; do
    [[ -n $key && -z $extra && ! ${audit_expected[$key]+yes} ]] || return 4
    if [[ $profile == audio-1mbps-embb-bidir10s-v2 ]]; then
      [[ -z $audit_schema || $audit_schema == AUDITv1 ]] || return 4
      audit_schema=AUDITv1
      [[ $rate == 4000 && $endpoint == 172.22.61.20 && $ip == 10.101.* ]] || return 4
      case "$key:$direction:$port" in u00-ul:ul:5201|u00-dl:dl:5203|u01-ul:ul:5202|u01-dl:dl:5204) ;; *) return 4;; esac
    elif [[ $profile == round-500-bidir17s-v3 ]]; then
      [[ -z $audit_schema || $audit_schema == AUDITSHARDv1 ]] || return 4
      audit_schema=AUDITSHARDv1
      [[ $key =~ ^u([0-4][0-9])-(ul|dl)$ && $direction == "${key##*-}" &&
         $endpoint =~ ^172\.22\.6[1-4]\.20$ && $port =~ ^[0-9]+$ && $port -ge 5200 && $port -le 6199 &&
         $ip =~ ^10\.10[1-4]\.[0-7]\.[0-9]{1,3}$ && $tun =~ ^(uesimtun|ueTun)[0-9]+$ &&
         $rate =~ ^[1-9][0-9]*$ && ! ${audit_flow_ports[$port]+yes} ]] || return 4
      index=${key:1:2}; audit_ue_directions[$index]+=$direction
    else
      return 4
    fi
    audit_expected[$key]=1; audit_flow_ports[$port]=1
  done <<< "$decoded"
  if [[ $audit_schema == AUDITv1 ]]; then
    (( ${#audit_expected[@]}>=1 && ${#audit_expected[@]}<=4 )) || return 4
  elif [[ $audit_schema == AUDITSHARDv1 ]]; then
    (( ${#audit_expected[@]}>=2 && ${#audit_expected[@]}<=100 && ${#audit_expected[@]}%2==0 )) || return 4
    for index in "${!audit_ue_directions[@]}"; do
      [[ ${audit_ue_directions[$index]} == uldl || ${audit_ue_directions[$index]} == dlul ]] || return 4
    done
    (( ${#audit_ue_directions[@]}*2==${#audit_expected[@]} )) || return 4
  else
    return 4
  fi
}
audit_size_limit() {
  [[ $1 == AUDITSHARDv1 ]] && printf '32768\n' || printf '8192\n'
}
resume_pending_audit() {
  local temporary=$directory/closure.pending mode schema actual_token boot ns extra kind original
  local owner_count=0 child_count=0 first second third limit before after value tail
  local -A found=() groups=() sockets=()
  [[ -f $temporary && ! -L $temporary ]] || return 4
  mode=$(stat -c '%u:%a:%h' "$temporary") || return 4
  [[ $mode == "$EUID:600:1" || $mode == "$EUID:400:1" ]] || return 4
  before=$(sha256sum "$temporary"); before=${before%% *}
  exec 6< "$temporary"
  read -r schema actual_token boot ns extra <&6 || return 4
  read -r kind original extra <&6 || return 4
  [[ ( $schema == AUDITv1 || $schema == AUDITSHARDv1 ) && $actual_token == "$token" && -z $extra &&
     $boot == "$(< /proc/sys/kernel/random/boot_id)" && $ns == "$(readlink /proc/self/ns/net)" &&
     $kind == MANIFEST && $original == "$(< "$directory/manifest")" ]] || return 4
  validate_audit_manifest "$original" || return 4
  [[ $schema == "$audit_schema" ]] || return 4
  limit=$(audit_size_limit "$schema") || return 4
  (( $(wc -c < "$temporary")<=limit )) || return 4
  while read -r kind first second third extra <&6; do
    case $kind in
      OWNER) [[ $first =~ ^[1-9][0-9]*$ && $second =~ ^[1-9][0-9]*$ && -z $third && -z $extra ]] || return 4
        ((owner_count+=1)); [[ $owner_count == 1 && ! -e /proc/$first/stat ]] || return 4;;
      CHILD) [[ ${audit_expected[$first]+yes} && ! ${found[$first]+yes} &&
          $second =~ ^[1-9][0-9]*$ && $third =~ ^[1-9][0-9]*$ && -z $extra &&
          ! ${groups[$second]+yes} ]] || return 4
        found[$first]=1; groups[$second]=1; ((child_count+=1))
        [[ ! -e /proc/$second/stat ]] && group_absent "$second" || return 4;;
      SOCKET) [[ $first =~ ^[1-9][0-9]*$ && -z $second && -z $third && -z $extra &&
          ! ${sockets[$first]+yes} ]] || return 4; sockets[$first]=1;;
      *) return 4;;
    esac
  done
  (( owner_count==1 && child_count==${#audit_expected[@]} )) || return 4
  after=$(sha256sum "$temporary"); after=${after%% *}
  [[ $before == "$after" && ! -L $temporary &&
     $(stat -c '%u:%a:%h' "$temporary") == "$mode" ]] || return 4
  chmod 400 "$temporary" && sync -- "$temporary" || return 4
  ln -- "$temporary" "$directory/closure.identity" && rm -- "$temporary" && sync -- "$directory" || return 4
}
retain_audit() {
  # New v2 tokens only. Legacy cleanup and old closed tokens are unchanged.
  [[ -e $directory/audit-v2 ]] || return 0
  [[ -d $directory && ! -L $directory && ! -L /tmp && ! -L $directory/audit-v2 ]] || return 4
  for command in stat readlink sha256sum sync; do command -v "$command" >/dev/null || return 4; done
  [[ $(stat -c '%u:%a' "$directory") == "$EUID:700" ]] || return 4
  if [[ -e $directory/closure.identity ]]; then
    [[ -f $directory/closure.identity && ! -L $directory/closure.identity &&
       $(stat -c '%u:%a:%h' "$directory/closure.identity") == "$EUID:400:1" ]] || return 4
    exec 7< "$directory/closure.identity"
    read -r schema actual_token boot ns extra <&7 || return 4
    read -r kind original extra <&7 || return 4
    [[ ( $schema == AUDITv1 || $schema == AUDITSHARDv1 ) && $actual_token == "$token" && -z $extra &&
       $boot == "$(< /proc/sys/kernel/random/boot_id)" && $ns == "$(readlink /proc/self/ns/net)" &&
       $kind == MANIFEST && $original == "$(< "$directory/manifest")" ]] || return 4
    validate_audit_manifest "$original" || return 4
    [[ $schema == "$audit_schema" ]] || return 4
    return 0
  fi
  for name in manifest owner children keys ready; do
    [[ -f $directory/$name && ! -L $directory/$name ]] || return 4
  done
  validate_audit_manifest "$(< "$directory/manifest")" || return 4
  if [[ -e $directory/closure.pending || -L $directory/closure.pending ]]; then
    resume_pending_audit
    return $?
  fi
  expected_keys=$(printf '%s' "$(< "$directory/manifest")" | base64 --decode | awk -F '\t' '{print $1}') || return 4
  [[ "$(< "$directory/keys")" == "$expected_keys" ]] || return 4
  local owner started extra key pid child_start value tail group link inode boot ns
  local -a fields
  local -A groups=() sockets=() seen=()
  read -r owner started extra < "$directory/owner"
  [[ $owner =~ ^[1-9][0-9]*$ && $started =~ ^[1-9][0-9]*$ && -z $extra ]] || return 4
  local body="OWNER $owner $started" count=0
  while read -r key pid child_start extra; do
    [[ ${audit_expected[$key]+yes} && $pid =~ ^[1-9][0-9]*$ && $child_start =~ ^[1-9][0-9]*$ && -z $extra && ! ${seen[$key]+yes} ]] || return 4
    seen[$key]=1; groups[$pid]=1; ((count+=1))
    body+=$'\n'"CHILD $key $pid $child_start"
  done < "$directory/children"
  (( count==${#audit_expected[@]} && count==$(wc -l < "$directory/keys") )) || return 4
  for path in /proc/[0-9]*/stat; do
    IFS= read -r value < "$path" 2>/dev/null || { [[ ! -e $path ]] && continue; return 4; }
    tail=${value##*) };read -ra fields <<< "$tail";group=${fields[2]}
    pid=${path#/proc/};pid=${pid%/stat}
    [[ $pid == "$owner" || ${groups[$group]+yes} ]] || continue
    for fd in /proc/"$pid"/fd/*; do
      [[ -L $fd ]] || continue
      link=$(readlink "$fd") || { [[ ! -L $fd && ! -e $fd ]] && continue; return 4; }
      if [[ $link =~ ^socket:\[([0-9]+)\]$ ]]; then sockets[${BASH_REMATCH[1]}]=1; fi
    done
  done
  IFS= read -r boot < /proc/sys/kernel/random/boot_id
  ns=$(readlink /proc/self/ns/net)
  [[ $boot =~ ^[a-f0-9-]{36}$ && $ns =~ ^net:\[[0-9]+\]$ ]] || return 4
  local temporary=$directory/closure.pending
  [[ ! -e $temporary && ! -L $temporary ]] || return 4
  ( set -o noclobber
    { printf '%s %s %s %s\n' "$audit_schema" "$token" "$boot" "$ns"
      printf 'MANIFEST %s\n' "$(< "$directory/manifest")"
      printf '%s\n' "$body"
      for inode in "${!sockets[@]}"; do printf 'SOCKET %s\n' "$inode"; done
    } > "$temporary"
  ) || return 4
  limit=$(audit_size_limit "$audit_schema") || return 4
  (( $(wc -c < "$temporary")<=limit )) || return 4
  chmod 400 "$temporary" && sync -- "$temporary" || return 4
  ln -- "$temporary" "$directory/closure.identity" && rm -- "$temporary" && sync -- "$directory" || return 4
}
if [[ $role == status ]]; then
  # Pure read-only path: no mkdir/flock/write/signal/cleanup trap or launch.
  for command in stat readlink sha256sum awk; do command -v "$command" >/dev/null || exit 5; done
  [[ ! -L /tmp && -d $directory && ! -L $directory && -d $directory/closed && ! -L $directory/closed &&
     -f $directory/closure.identity && ! -L $directory/closure.identity &&
     $(stat -c '%u:%a' "$directory") == "$EUID:700" &&
     $(stat -c '%u:%a:%h' "$directory/closure.identity") == "$EUID:400:1" ]] || exit 4
  before=$(sha256sum "$directory/closure.identity");before=${before%% *}
  exec 8< "$directory/closure.identity"
  read -r schema actual_token boot ns extra <&8
  [[ ( $schema == AUDITv1 || $schema == AUDITSHARDv1 ) && $actual_token == "$token" && -z $extra &&
     $boot == "$(< /proc/sys/kernel/random/boot_id)" && $ns == "$(readlink /proc/self/ns/net)" ]] || exit 4
  limit=$(audit_size_limit "$schema") || exit 4
  (( $(wc -c < "$directory/closure.identity")<=limit )) || exit 4
  read -r kind original extra <&8
  [[ $kind == MANIFEST && $original == "$manifest" && -z $extra ]] || exit 4
  validate_audit_manifest "$manifest" || exit 4
  [[ $schema == "$audit_schema" ]] || exit 4
  declare -A found=() groups=() sockets=() flow_ports=()
  for port in "${!audit_flow_ports[@]}"; do printf -v hexport '%04X' "$port"; flow_ports[$hexport]=1; done
  owners=0
  while read -r kind first second third extra <&8; do
    case $kind in
      OWNER) [[ $first =~ ^[1-9][0-9]*$ && $second =~ ^[1-9][0-9]*$ && -z $third && -z $extra ]] || exit 4
        ((owners+=1)); [[ ! -e /proc/$first/stat ]] || exit 4;;
      CHILD) [[ ${audit_expected[$first]+yes} && ! ${found[$first]+yes} && $second =~ ^[1-9][0-9]*$ &&
          $third =~ ^[1-9][0-9]*$ && -z $extra && ! ${groups[$second]+yes} ]] || exit 4
        found[$first]=1;groups[$second]=1; [[ ! -e /proc/$second/stat ]] || exit 4;;
      SOCKET) [[ $first =~ ^[1-9][0-9]*$ && -z $second && -z $third && -z $extra && ! ${sockets[$first]+yes} ]] || exit 4
        sockets[$first]=1;;
      *) exit 4;;
    esac
  done
  (( owners==1 && ${#found[@]}==${#audit_expected[@]} )) || exit 4
  for path in /proc/[0-9]*/stat; do
    IFS= read -r value < "$path" 2>/dev/null || { [[ ! -e $path ]] && continue; exit 4; }
    tail=${value##*) };read -ra fields <<< "$tail"
    [[ ${fields[2]} =~ ^[0-9]+$ && ! ${groups[${fields[2]}]+yes} ]] || exit 4
  done
  for netfile in /proc/net/tcp /proc/net/tcp6 /proc/net/udp /proc/net/udp6; do
    [[ -r $netfile ]] || exit 4
    # Full CURRENT tuple space, not a claimed inventory of historical inodes.
    # Local OR remote fixed port covers listener/control/UDP/reverse flows,
    # even after descriptor transfer outside the recorded process group.
    rows=$(cat "$netfile") || exit 4
    header=1; width=8; [[ $netfile != *6 ]] || width=32
    while read -ra fields; do
      if (( header )); then
        [[ ${fields[0]-} == sl && ${fields[1]-} == local_address &&
           ( ${fields[2]-} == rem_address || ${fields[2]-} == remote_address ) && ${fields[3]-} == st ]] || exit 4
        header=0; continue
      fi
      (( ${#fields[@]}>=10 )) || exit 4
      [[ ${fields[0]} =~ ^[0-9]+:$ && ${fields[1]} =~ ^[A-F0-9]+:[A-F0-9]{4}$ &&
         ${fields[2]} =~ ^[A-F0-9]+:[A-F0-9]{4}$ && ${fields[3]} =~ ^[A-F0-9]{2}$ &&
         ${fields[4]} =~ ^[A-F0-9]{8}:[A-F0-9]{8}$ && ${fields[5]} =~ ^[A-F0-9]{2}:[A-F0-9]+$ &&
         ${fields[6]} =~ ^[A-F0-9]{8}$ && ${fields[7]} =~ ^[0-9]+$ &&
         ${fields[8]} =~ ^[0-9]+$ && ${fields[9]} =~ ^[0-9]+$ ]] || exit 4
      if [[ $netfile == /proc/net/tcp* ]]; then
        [[ ${fields[3]} =~ ^0[1-9ABC]$ ]] || exit 4
      else
        [[ ${fields[3]} == 01 || ${fields[3]} == 07 ]] || exit 4
      fi
      local_ip=${fields[1]%:*};remote_ip=${fields[2]%:*}
      (( ${#local_ip}==width && ${#remote_ip}==width )) || exit 4
      inode=${fields[9]}; [[ ! ${sockets[$inode]+yes} ]] || exit 4
      local_port=${fields[1]##*:};remote_port=${fields[2]##*:}
      if [[ ${flow_ports[$local_port]+yes} || ${flow_ports[$remote_port]+yes} ]]; then
        # Kernel TIME_WAIT has no userspace socket inode. Never exempt UDP,
        # another state, or a TIME_WAIT row with a live inode.
        [[ $netfile == /proc/net/tcp* && ${fields[3]} == 06 && $inode == 0 ]] || exit 4
      fi
    done <<< "$rows"
    (( header==0 )) || exit 4
  done
  # Also reject a retained socket descriptor inherited outside the old group,
  # including non-IP families. An unreadable surviving fd directory is unknown.
  if (( ${#sockets[@]} )); then
    for procdir in /proc/[0-9]*; do
      [[ -d $procdir ]] || continue
      [[ -r $procdir/fd && -x $procdir/fd ]] || { [[ ! -e $procdir ]] && continue; exit 4; }
      for fd in "$procdir"/fd/*; do
        [[ -L $fd ]] || continue
        link=$(readlink "$fd") || { [[ ! -L $fd && ! -e $fd ]] && continue; exit 4; }
        if [[ $link =~ ^socket:\[([0-9]+)\]$ ]]; then
          [[ ! ${sockets[${BASH_REMATCH[1]}]+yes} ]] || exit 4
        fi
      done
    done
  fi
  after=$(sha256sum "$directory/closure.identity");after=${after%% *}
  [[ $before == "$after" && ! -L $directory/closure.identity &&
     $boot == "$(< /proc/sys/kernel/random/boot_id)" && $ns == "$(readlink /proc/self/ns/net)" &&
     $(stat -c '%u:%a:%h' "$directory/closure.identity") == "$EUID:400:1" ]] || exit 4
  printf 'ABSENTv2 %s %s\n' "$token" "$before"
  exit 0
fi
if [[ $role == ready ]]; then
  if [[ -d $directory && ! -L $directory && -f $directory/ready ]]; then printf 'READY\n'; else printf 'PENDING\n'; fi
  exit 0
fi
if [[ $role == client_ready || $role == release ]]; then
  if [[ $role == client_ready && ! -e $directory ]]; then printf 'PENDING\n'; exit 0; fi
  [[ -d $directory && ! -L $directory && ! -e $directory/closed && ! -e $directory/cancelling ]] || exit 4
  exec 9> "$directory/control.lock"
  flock -w 2 9 || exit 4
  [[ ! -e $directory/closed && ! -e $directory/cancelling ]] || exit 4
  [[ -f $directory/manifest && ! -L $directory/manifest && -d $directory/client ]] || { printf 'PENDING\n'; exit 0; }
  [[ $(< "$directory/manifest") == "$manifest" ]] || exit 4
  [[ -f $directory/ready && -f $directory/owner && -f $directory/children && -f $directory/keys ]] || { printf 'PENDING\n'; exit 0; }
  for name in ready owner children keys; do [[ ! -L $directory/$name ]] || exit 4; done
  read -r owner_pid owner_start < "$directory/owner"
  [[ $owner_pid =~ ^[0-9]+$ && $owner_start =~ ^[0-9]+$ ]] && live_process "$owner_pid" "$owner_start" || exit 4
  expected_keys=$(printf '%s' "$manifest" | base64 --decode | awk -F '\t' '{print $1}') || exit 4
  [[ $(< "$directory/keys") == "$expected_keys" ]] || exit 4
  declare -A expected=() seen=() leaders=() armed=()
  while IFS= read -r key; do
    [[ $key =~ ^u[0-4][0-9]-(ul|dl)$ && ! ${expected[$key]+present} ]] || exit 4
    expected[$key]=1
  done <<< "$expected_keys"
  (( ${#expected[@]} >= 1 && ${#expected[@]} <= 100 )) || exit 4
  while read -r key pid started extra; do
    [[ $key =~ ^u[0-4][0-9]-(ul|dl)$ && $pid =~ ^[0-9]+$ && $started =~ ^[0-9]+$ && -z $extra ]] || exit 4
    [[ ${expected[$key]+present} && ! ${seen[$key]+present} && ! ${leaders[$pid]+present} ]] || exit 4
    seen[$key]=1; leaders[$pid]=1
    live_process "$pid" "$started" "$pid" || exit 4
    [[ -f $directory/$key.armed && ! -L $directory/$key.armed ]] || exit 4
    read -r armed_pid armed_start armed_group extra < "$directory/$key.armed"
    [[ $armed_pid =~ ^[0-9]+$ && $armed_start =~ ^[0-9]+$ && $armed_group == "$pid" && -z $extra ]] || exit 4
    [[ ! ${armed[$armed_pid]+present} ]] || exit 4
    armed[$armed_pid]=1
    live_process "$armed_pid" "$armed_start" "$pid" || exit 4
  done < "$directory/children"
  (( ${#seen[@]} == ${#expected[@]} )) || exit 4
  if [[ $role == client_ready ]]; then printf 'READY\n'; exit 0; fi
  now=$(date +%s)
  (( epoch >= now + 2 && epoch <= now + 10 )) || exit 8
  # Atomic one-shot claim. A partial publication remains quarantined, not retried.
  mkdir "$directory/release-claimed" || exit 13
  [[ ! -e $directory/cancelling && ! -e $directory/closed && $(start_time "$owner_pid") == "$owner_start" ]] || exit 4
  printf '%s\n' "$epoch" > "$directory/release-claimed/epoch"
  mv "$directory/release-claimed/epoch" "$directory/release"
  printf 'RELEASED\n'
  exit 0
fi
if [[ $role == cancel ]]; then
  # Atomically reserve even an absent token: a delayed pre-crash exec must
  # never launch after recovery declared this token closed.
  if mkdir "$directory" 2>/dev/null; then
    mkdir "$directory/closed"
    exit 0
  fi
  [[ -d $directory && ! -L $directory ]] || exit 4
  [[ ! -d $directory/closed || -L $directory/closed ]] || exit 0
  exec 9> "$directory/control.lock"
  flock -w 2 9 || exit 4
  mkdir "$directory/cancelling" 2>/dev/null || true
  flock -u 9
  [[ -d $directory && ! -L $directory && -f $directory/owner ]] || exit 4
  read -r owner_pid owner_start < "$directory/owner"
  [[ $owner_pid =~ ^[0-9]+$ && $owner_start =~ ^[0-9]+$ ]] || exit 4
  if [[ -e /proc/$owner_pid/stat && $(start_time "$owner_pid") != "$owner_start" ]]; then exit 4; fi
  if [[ -e /proc/$owner_pid/stat && $(awk '{print $3}' "/proc/$owner_pid/stat") != Z ]]; then
    kill -TERM "$owner_pid"
    for ((attempt=0;attempt<30;attempt++)); do
      [[ ! -d $directory/closed || -L $directory/closed ]] || exit 0
      sleep 0.1
    done
    exit 4
  fi
  # A ready marker certifies that every child PID/start tuple was persisted.
  # A crash before this marker remains quarantined, never guessed clean.
  [[ -f $directory/ready && -f $directory/children && -f $directory/keys ]] || exit 4
  [[ $(wc -l < "$directory/children") == "$(wc -l < "$directory/keys")" ]] || exit 4
  while read -r key pid started; do
    [[ $key =~ ^u[0-4][0-9]-(ul|dl)$ && $pid =~ ^[0-9]+$ && $started =~ ^[0-9]+$ ]] || exit 4
    if [[ -e /proc/$pid/stat ]]; then
      [[ $(start_time "$pid") == "$started" ]] || exit 4
      kill -TERM -- "-$pid" 2>/dev/null || true
    fi
  done < "$directory/children"
  # Completed workers normally have no remaining process groups. A fixed
  # grace sleep then ages valid measurements without helping cleanup. Keep
  # the full TERM grace whenever ANY recorded group still exists, including
  # zombies; the unchanged final PID/start and group checks remain mandatory.
  needs_grace=0
  while read -r key pid started; do
    if ! group_absent "$pid"; then needs_grace=1; break; fi
  done < "$directory/children"
  if (( needs_grace )); then sleep 2.2; fi
  while read -r key pid started; do
    if [[ -e /proc/$pid/stat ]]; then
      [[ $(start_time "$pid") == "$started" ]] || exit 4
      [[ $(awk '{print $3}' "/proc/$pid/stat") == Z ]] || exit 4
    fi
    group_absent "$pid" || exit 4
  done < "$directory/children"
  # This atomic marker is written only after every recorded PGID is absent.
  # Keep it permanently for this Pod lifetime; never reopen a used token.
  retain_audit || exit 4
  mkdir "$directory/closed"
  while read -r key; do
    [[ $key =~ ^u[0-4][0-9]-(ul|dl)$ ]] || exit 4
    rm -f -- "$directory/$key.json" "$directory/$key.meta" "$directory/$key.err"
  done < "$directory/keys"
  rm -f -- "$directory/ready" "$directory/owner" "$directory/children" "$directory/keys"
  exit 0
fi
for dependency in awk base64 date iperf3 setsid timeout wc sleep mv flock; do
  command -v "$dependency" >/dev/null || exit 5
done
umask 077
# Bound new work, not recovery. Never prune closed tokens: an old pending exec
# may still carry one. Cancellation must still be able to close its own token.
token_count=0
for token_directory in /tmp/cityverse-batch-*; do
  [[ -d $token_directory ]] || continue
  ((token_count+=1))
done
(( token_count < 4096 )) || exit 12
mkdir "$directory" || exit 6
printf '%s\n' "$manifest" > "$directory/manifest"
[[ $role != client ]] || mkdir "$directory/client"
declare -a keys=() pids=() starts=() endpoints=() ports=() ips=() tuns=() directions=() rates=()
listener_ports=
printf '%s %s\n' "$$" "$(start_time "$$")" > "$directory/owner"
cleanup() {
  trap - EXIT TERM INT
  local index pid current unresolved=0
  mkdir "$directory/cancelling" 2>/dev/null || true
  for index in "${!pids[@]}"; do
    pid=${pids[$index]}
    if [[ -e /proc/$pid/stat ]]; then
      current=$(start_time "$pid") || continue
      if [[ $current != "${starts[$index]}" ]]; then unresolved=1; continue; fi
      kill -TERM -- "-$pid" 2>/dev/null || true
    fi
  done
  # timeout --kill-after owns a final bound even if the exec channel disappears.
  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
  # Reaping the timeout leader alone says nothing about surviving descendants.
  # Preserve exact token/children records when a group cannot be proved absent.
  sleep 0.1
  for pid in "${pids[@]}"; do group_absent "$pid" || unresolved=1; done
  (( unresolved == 0 )) || return 4
  retain_audit || return 4
  mkdir "$directory/closed"
  for index in "${keys[@]}"; do
    rm -f -- "$directory/$index.json" "$directory/$index.meta" "$directory/$index.err"
  done
  rm -f -- "$directory/ready" "$directory/owner" "$directory/children" "$directory/keys"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
durations=()
while IFS=$'\t' read -r key endpoint port ip tun direction rate profile extra; do
  [[ $key =~ ^u[0-4][0-9]-(ul|dl)$ && $endpoint =~ ^172\.22\.6[1-4]\.20$ ]] || exit 7
  [[ $port =~ ^52[0-9][0-9]$ && $ip =~ ^10\.10[1-4]\.[0-7]\.[0-9]{1,3}$ ]] || exit 7
  [[ $tun =~ ^uesimtun[0-9]+$ && $direction =~ ^(ul|dl)$ && $rate =~ ^[0-9]+$ ]] || exit 7
  (( rate >= 1 && rate <= 10000 )) || exit 7
  duration=5
  [[ -z $extra ]] || exit 7
  if [[ -n $profile ]]; then
    [[ $profile == audio-1mbps-embb-bidir10s-v2 && $rate == 4000 && $endpoint == 172.22.61.20 && $ip == 10.101.* ]] || exit 7
    case "$key:$direction:$port" in u00-ul:ul:5201|u00-dl:dl:5203|u01-ul:ul:5202|u01-dl:dl:5204) ;; *) exit 7;; esac
    duration=10
  fi
  keys+=("$key"); endpoints+=("$endpoint"); ports+=("$port"); ips+=("$ip")
  tuns+=("$tun"); directions+=("$direction"); rates+=("$rate")
  durations+=("$duration")
  printf -v hexport '%04X' "$port"
  listener_ports+="${listener_ports:+,}$hexport"
done < <(printf '%s' "$manifest" | base64 --decode)
(( ${#keys[@]} >= 1 && ${#keys[@]} <= 100 )) || exit 7
 if [[ ${durations[0]} == 10 || ${durations[0]} == 17 ]]; then
  printf 'v2\n' > "$directory/audit-v2"
fi
printf '%s\n' "${keys[@]}" > "$directory/keys"
: > "$directory/children"
[[ $role != client || $epoch == 0 ]] || exit 8
# Child expands these expressions in its own process, not in the parent.
# shellcheck disable=SC2016
child='set -u; directory=$1; key=$2; role=$3; shift 3
owner_live() {
  local value tail recorded_pid recorded_start extra
  local -a fields
  read -r recorded_pid recorded_start extra < "$directory/owner" || return 1
  [[ $recorded_pid == "$owner_pid" && $recorded_start == "$owner_start" && -z $extra ]] || return 1
  IFS= read -r value < "/proc/$owner_pid/stat" || return 1
  tail=${value##*) }; read -ra fields <<< "$tail"
  [[ ${fields[0]} != Z && ${fields[0]} != X && ${fields[19]} == "$owner_start" ]]
}
if [[ $role == client ]]; then
  read -r owner_pid owner_start < "$directory/owner" || exit 4
  read -r value < /proc/$$/stat; tail=${value##*) }; read -ra fields <<< "$tail"
  printf "%s %s %s\n" "$$" "${fields[19]}" "${fields[2]}" > "$directory/$key.armed"
  until [[ -f $directory/release ]]; do
    [[ ! -e $directory/cancelling && ! -e $directory/closed && $SECONDS -lt 20 ]] || exit 4
    owner_live || exit 4
    sleep 0.05
  done
  read -r epoch < "$directory/release"
  [[ $epoch =~ ^[0-9]+$ ]] || exit 8
  now=$EPOCHSECONDS
  (( epoch >= now + 2 && epoch <= now + 10 )) || exit 8
  while (( EPOCHSECONDS < epoch )); do
    [[ ! -e $directory/cancelling && ! -e $directory/closed && $SECONDS -lt 24 ]] || exit 4
    owner_live || exit 4
    sleep 0.02
  done
  (( EPOCHSECONDS <= epoch )) || exit 8
  [[ ! -e $directory/cancelling && ! -e $directory/closed ]] || exit 4
  owner_live || exit 4
fi
started=$(date +%s.%N)
# Read/check and command execution are not atomic with supervisor death.
if [[ $role == client ]]; then owner_live || exit 4; fi
"$@" > "$directory/$key.json" 2> "$directory/$key.err"; status=$?; finished=$(date +%s.%N); printf "%s %s %s\n" "$started" "$finished" "$status" > "$directory/$key.meta"'
listener_snapshot() {
  local require_all=$1
  awk -v ports="$listener_ports" -v require_all="$require_all" '
    BEGIN {
      count=split(ports, values, ",")
      for (i=1; i<=count; i++) wanted[values[i]]=1
    }
    $4 == "0A" {
      split($2, address, ":")
      port=address[length(address)]
      if (port in wanted) seen[port]=1
    }
    END {
      if (require_all) {
        for (port in wanted) if (!(port in seen)) exit 1
        exit 0
      }
      for (port in seen) exit 0
      exit 1
    }
  ' /proc/net/tcp
}
if [[ $role == server ]] && listener_snapshot 0; then exit 9; fi
for index in "${!keys[@]}"; do
  key=${keys[$index]}; port=${ports[$index]}; endpoint=${endpoints[$index]}
  if [[ $role == server ]]; then
    command=(iperf3 -s -1 -B "$endpoint" -p "$port" -J)
  else
    command=(iperf3 -c "$endpoint" -p "$port" -u -b "$((${rates[$index]} * 1000))" -l 1200 -t "${durations[$index]}" -B "${ips[$index]}" --bind-dev "${tuns[$index]}" -J --get-server-output)
    [[ ${directions[$index]} != dl ]] || command+=(-R)
  fi
  setsid timeout --signal=TERM --kill-after=2 30 bash -c "$child" batch-child "$directory" "$key" "$role" "${command[@]}" &
  pid=$!; pids+=("$pid"); starts+=("$(start_time "$pid")")
  printf '%s %s %s\n' "$key" "$pid" "${starts[-1]}" >> "$directory/children"
done
if [[ $role == server ]]; then
  for ((attempt=0;attempt<50;attempt++)); do
    listener_snapshot 1 && { ready=1; break; }
    ready=0
    sleep 0.1
  done
  (( ready == 1 )) || exit 10
  printf 'ready\n' > "$directory/ready"
else
  for ((attempt=0;attempt<80;attempt++)); do
    ready=1
    for key in "${keys[@]}"; do [[ -f $directory/$key.armed ]] || ready=0; done
    (( ready == 0 )) || break
    sleep 0.05
  done
  (( ready == 1 )) || exit 10
  printf 'ready\n' > "$directory/ready"
fi
for pid in "${pids[@]}"; do wait "$pid" || true; done
for key in "${keys[@]}"; do
  [[ -f $directory/$key.meta && -f $directory/$key.json ]] || exit 11
  (( $(wc -c < "$directory/$key.json") <= 1048576 )) || exit 11
  read -r started finished status < "$directory/$key.meta"
  printf 'BATCHv1\t%s\t%s\t%s\t%s\t' "$key" "$started" "$finished" "$status"
  base64 -w0 "$directory/$key.json"
  printf '\n'
done
'''


_LEGACY_TUN_CHECK = '^uesimtun[0-9]+$'
_PINNED_PORT_GUARD = r'[[ $port =~ ^52[0-9][0-9]$ && $ip =~ ^10\.10[1-4]\.[0-7]\.[0-9]{1,3}$ ]] || exit 7'
_SHARD_PORT_GUARD = r'[[ $port =~ ^(5[2-9]|6[01])[0-9][0-9]$ && $ip =~ ^10\.10[1-4]\.[0-7]\.[0-9]{1,3}$ ]] || exit 7'
_PINNED_PROFILE_GUARD = '''  duration=5
  [[ -z $extra ]] || exit 7
  if [[ -n $profile ]]; then
    [[ $profile == audio-1mbps-embb-bidir10s-v2 && $rate == 4000 && $endpoint == 172.22.61.20 && $ip == 10.101.* ]] || exit 7
    case "$key:$direction:$port" in u00-ul:ul:5201|u00-dl:dl:5203|u01-ul:ul:5202|u01-dl:dl:5204) ;; *) exit 7;; esac
    duration=10
  fi'''
_PINNED_SHARD_AUDIT_PROFILE = 'elif [[ $profile == round-500-bidir17s-v3 ]]; then'
_SHARD_AUDIT_PROFILE = 'elif [[ $profile == round-500-bidir18s-v4 ]]; then'
_PINNED_AUDIT_MARKER = 'if [[ ${durations[0]} == 10 || ${durations[0]} == 17 ]]; then'
_SHARD_AUDIT_MARKER = 'if [[ ${durations[0]} == 10 || ${durations[0]} == 18 ]]; then'
_PINNED_SERVER_READINESS = 'for ((attempt=0;attempt<50;attempt++)); do\n    listener_snapshot 1'
_SHARD_SERVER_READINESS = f'for ((attempt=0;attempt<{SHARD_SERVER_READINESS_ATTEMPTS};attempt++)); do\n    listener_snapshot 1'
_PINNED_CANCEL_READY_GATE = '[[ -f $directory/ready && -f $directory/children && -f $directory/keys ]] || exit 4'
_SHARD_CANCEL_COMPLETE_GATE = '[[ -f $directory/children && -f $directory/keys ]] || exit 4'
_PINNED_AUDIT_FILES = 'for name in manifest owner children keys ready; do'
_SHARD_AUDIT_FILES = 'for name in manifest owner children keys; do'
_SHARD_PROFILE_GUARD = '''  [[ $profile == round-500-bidir18s-v4 && -z $extra ]] || exit 7
  duration=18'''
_CLIENT_LAUNCH_POINT = '''  owner_live || exit 4
fi
started=$(date +%s.%N)'''
_CHILD_TIMEOUT = 'setsid timeout --signal=TERM --kill-after=2 30 bash -c'
SHARD_CLIENT_CHILD_TIMEOUT_SECONDS = 40
SHARD_SERVER_CHILD_TIMEOUT_SECONDS = 48
_SHARD_CHILD_TIMEOUT = f'''child_timeout={SHARD_CLIENT_CHILD_TIMEOUT_SECONDS}
  [[ $role != server ]] || child_timeout={SHARD_SERVER_CHILD_TIMEOUT_SECONDS}
  setsid timeout --signal=TERM --kill-after=2 "$child_timeout" bash -c'''
_SHARD_CLIENT_LAUNCH = '''  owner_live || exit 4
  # Fixed code derives a bounded slot from the already validated flow key.
  # Ten UE pairs occupy slots 0..19; larger shards reuse those slots rather
  # than accepting a caller-controlled delay or extending the launch window.
  ue_index=${key:2:1}
  direction_slot=0; [[ ${key##*-} != dl ]] || direction_slot=1
  launch_slot=$((ue_index * 2 + direction_slot))
  printf -v launch_delay '0.%03d' "$((launch_slot * __INTERVAL_MS__))"
  sleep "$launch_delay"
  [[ ! -e $directory/cancelling && ! -e $directory/closed && $SECONDS -lt 25 ]] || exit 4
  owner_live || exit 4
fi
started=$(date +%s.%N)'''.replace('__INTERVAL_MS__', str(SHARD_LAUNCH_INTERVAL_MS))


def _fixed_worker_variant(*replacements):
    """Derive reviewed fixed scripts without changing the pinned legacy bytes."""
    script = WORKER_SCRIPT
    for old, new in replacements:
        if script.count(old) != 1:
            raise RuntimeError('Pinned worker source for fixed profile changed')
        script = script.replace(old, new)
    return script


FREE_RAN_WORKER_SCRIPT = _fixed_worker_variant((_LEGACY_TUN_CHECK, '^ueTun[0-9]+$'))
SHARD_WORKER_SCRIPT = _fixed_worker_variant((_PINNED_PORT_GUARD, _SHARD_PORT_GUARD),
    (_PINNED_PROFILE_GUARD, _SHARD_PROFILE_GUARD),
    (_PINNED_SHARD_AUDIT_PROFILE, _SHARD_AUDIT_PROFILE),
    (_PINNED_AUDIT_MARKER, _SHARD_AUDIT_MARKER),
    (_PINNED_SERVER_READINESS, _SHARD_SERVER_READINESS),
    (_PINNED_CANCEL_READY_GATE, _SHARD_CANCEL_COMPLETE_GATE),
    (_PINNED_AUDIT_FILES, _SHARD_AUDIT_FILES),
    (_CLIENT_LAUNCH_POINT, _SHARD_CLIENT_LAUNCH),
    (_CHILD_TIMEOUT, _SHARD_CHILD_TIMEOUT))
FREE_RAN_SHARD_WORKER_SCRIPT = _fixed_worker_variant((_PINNED_PORT_GUARD, _SHARD_PORT_GUARD),
    (_PINNED_PROFILE_GUARD, _SHARD_PROFILE_GUARD), (_LEGACY_TUN_CHECK, '^ueTun[0-9]+$'),
    (_PINNED_SHARD_AUDIT_PROFILE, _SHARD_AUDIT_PROFILE),
    (_PINNED_AUDIT_MARKER, _SHARD_AUDIT_MARKER),
    (_PINNED_SERVER_READINESS, _SHARD_SERVER_READINESS),
    (_PINNED_CANCEL_READY_GATE, _SHARD_CANCEL_COMPLETE_GATE),
    (_PINNED_AUDIT_FILES, _SHARD_AUDIT_FILES),
    (_CLIENT_LAUNCH_POINT, _SHARD_CLIENT_LAUNCH),
    (_CHILD_TIMEOUT, _SHARD_CHILD_TIMEOUT))


def worker_command(role, token, jobs=(), *, epoch=0, profile=LEGACY_PROFILE):
    """Only fixed shell code plus validated argv; never eval/interpolate inputs."""
    v2=_profile(profile)
    if v2:
        from archive.legacy.scenarios.commissioning import validate_jobs
        validate_jobs(jobs,partial=True)
    elif any(j.duration != (SHARD_DURATION_SECONDS if profile == SHARD_PROFILE else 5) for j in jobs):
        raise PolicyBlocked('Worker duration does not match profile')
    _text(token, r'[a-f0-9]{32}', 'worker token')
    if role not in ('server', 'client', 'ready', 'client_ready', 'release', 'cancel', 'status') or type(epoch) is not int or epoch < 0:
        raise PolicyBlocked('Invalid worker mode or schedule')
    if role=='status' and (profile not in (COMMISSIONING_PROFILE, SHARD_PROFILE) or not jobs or epoch!=0):
        raise PolicyBlocked('Readonly status requires an exact retained-audit manifest')
    if role in ('server', 'client', 'client_ready', 'release') and (not jobs or len(jobs) > 100):
        raise PolicyBlocked('Empty or oversized worker group')
    families = {('free-ran-ue' if re.fullmatch(r'ueTun[0-9]+', j.tun) else 'ueransim') for j in jobs}
    if len(families) > 1:
        raise PolicyBlocked('Mixed RAN TUN families cannot share a worker group')
    script = ((FREE_RAN_SHARD_WORKER_SCRIPT if families == {'free-ran-ue'} else SHARD_WORKER_SCRIPT)
              if profile == SHARD_PROFILE else
              (FREE_RAN_WORKER_SCRIPT if families == {'free-ran-ue'} else WORKER_SCRIPT))
    manifest = worker_manifest(jobs, profile=profile)
    return ['bash', '-c', script, 'cityverse-batch', role, token, str(epoch),
            base64.b64encode(manifest.encode()).decode()]


def worker_manifest(jobs, *, profile=LEGACY_PROFILE):
    """Return the exact immutable TSV bytes supplied to a worker."""
    suffix = '\t' + profile if profile != LEGACY_PROFILE else ''
    return ''.join(f'{j.key}\t{j.endpoint}\t{j.port}\t{j.ue_ip}\t{j.tun}\t{j.direction}\t{j.offered_kbps}{suffix}\n'
                   for j in jobs)


def parse_worker_output(output, expected_keys):
    records = {}
    for line in output.splitlines():
        fields = line.split('\t')
        if len(fields) != 6 or fields[0] != 'BATCHv1' or fields[1] not in expected_keys or fields[1] in records:
            raise PolicyBlocked('Malformed, foreign or duplicate worker output')
        try:
            raw = base64.b64decode(fields[5], validate=True)
            if len(raw) > 1_048_576:
                raise ValueError('Oversized worker record')
            records[fields[1]] = {'startedAt': float(fields[2]), 'finishedAt': float(fields[3]),
                                  'exit': int(fields[4]), 'report': json.loads(raw)}
        except (ValueError, TypeError) as exc:
            raise PolicyBlocked('Invalid actual worker record') from exc
    if set(records) != set(expected_keys):
        raise PolicyBlocked('Incomplete worker result set')
    return records


class BatchTraffic:
    """Execution coordinator with mandatory external ownership/native fences.

    `execute(pod, uid, argv, timeout)` MUST UID-fence the exact pinned traffic
    container before and after remote execution. `fence()` is called on every
    operation (including cleanup); `native_guard(jobs)` compares ALL immutable
    identities, including distinct NAS session ID/PFCP SEID, full SMF context ref, DNN/S-NSSAI and
    UPF observer epoch, against fresh trusted native snapshots. Same-IP/TUN
    reconnects must fail. No default unfenced transport or opt-in exists.
    """
    def __init__(self, *, execute, fence, native_guard, failure_reporter=None, diagnostic_reporter=None, clock=time.time, sleep=time.sleep,
                 monotonic=time.monotonic, cancelled=lambda: False, cleanup_observer=None, operation_observer=None,
                 release_barrier=None):
        if not all(callable(v) for v in (execute, fence, native_guard, clock, sleep, monotonic, cancelled)):
            raise PolicyBlocked('Explicit batch ownership guards are mandatory')
        self.execute, self.fence, self.native_guard = execute, fence, native_guard
        self.failure_reporter = failure_reporter
        self.diagnostic_reporter = diagnostic_reporter
        self.clock, self.sleep = clock, sleep
        self.monotonic, self.cancelled = monotonic, cancelled
        if release_barrier is not None and not callable(release_barrier):
            raise PolicyBlocked('Invalid common release barrier')
        self.release_barrier = release_barrier
        if cleanup_observer is not None and not callable(cleanup_observer):
            raise PolicyBlocked('Invalid cleanup lifecycle observer')
        self.cleanup_observer = cleanup_observer
        if operation_observer is not None and not callable(operation_observer):
            raise PolicyBlocked('Invalid operation lifecycle observer')
        self.operation_observer = operation_observer
        self._operation_used = False

    def _cancel_all(self, identities, token, *, profile=LEGACY_PROFILE):
        """Bounded parallel exact-owner cancellation; drain every submitted call."""
        def cancel(identity):
            if self.fence() is not True:
                raise PolicyBlocked('Recovery ownership fence rejected operation')
            self.execute(*identity, worker_command('cancel', token, profile=profile), 38)
        errors = []
        # Four slice UE workers plus four N6 workers can finish in one wave.
        # Keep a fixed cap even when recovery receives a larger identity set.
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [(identity, pool.submit(cancel, identity)) for identity in sorted(set(identities))]
            for identity, future in futures:
                try:
                    future.result()
                except Exception as exc:
                    # Recovery failures need a bounded reason for operators;
                    # never log a subprocess command, response body or token.
                    reason = str(exc)
                    if (type(exc).__name__ not in {"RoundRuntimeBlocked", "BatchBlocked", "PolicyBlocked"}
                            or re.fullmatch(r"[A-Za-z0-9 ._:-]{1,120}", reason) is None):
                        reason = "redacted"
                    logging.getLogger(__name__).warning(
                        "Batch exact-owner cancel failed pod=%s type=%s reason=%s reasonSha256=%s",
                        identity[0], type(exc).__name__, reason,
                        hashlib.sha256(str(exc).encode()).hexdigest())
                    errors.append(identity)
        return errors

    @staticmethod
    def _worker_output(future, identity, role, keys, deadline):
        """Preserve bounded wait/parse diagnostics without recording output/secrets."""
        started = time.monotonic()
        budget = max(0, deadline - started)
        substage = 'wait'
        try:
            output = future.result(timeout=budget)
            substage = 'parse'
            return parse_worker_output(output, keys)
        except Exception as exc:
            # Do not replace the original error or its transport diagnostic.
            category = ('timeout' if isinstance(exc, TimeoutError) else
                        'blocked' if isinstance(exc, PolicyBlocked) else
                        'invalid_value' if isinstance(exc, ValueError) else 'other')
            exc.worker_diagnostic = {
                'kind': 'worker_result', 'role': role, 'pod': identity[0],
                'podUid': identity[1], 'jobKeys': sorted(keys),
                'substage': substage, 'errorCategory': category,
                'elapsedSeconds': max(0, time.monotonic() - started),
                'budgetSeconds': budget,
            }
            raise

    def _catch_up(self, records):
        """Wait at most one monotonic second; never rewrite remote evidence.

        Native nodes may be slightly ahead of the observer. This is not an
        acceptance tolerance: reduction still requires zero future timestamps.
        """
        values=[row.get(k) for row in records.values() for k in ('finishedAt','receiverFinishedAt')]
        now=self.clock()
        if not values or any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in [now,*values]):
            raise BatchBlocked('batch.worker_timestamp_invalid','Invalid clock catch-up timestamp')
        latest=max(values)
        if latest-now > 1:
            raise BatchBlocked('batch.window_unverified','Future worker evidence exceeds bounded clock catch-up')
        deadline=self.monotonic()+1
        while now < latest:
            remaining=deadline-self.monotonic()
            if remaining <= 0 or self.cancelled() or self.fence() is not True:
                raise BatchBlocked('batch.window_unverified','Clock catch-up expired or ownership changed')
            self.sleep(min(.02,remaining,latest-now))
            now=self.clock()
            if not math.isfinite(now) or now < 0 or self.monotonic() > deadline:
                raise BatchBlocked('batch.window_unverified','Clock catch-up clock or deadline invalid')

    def recover(self, bindings, *, token, profile=LEGACY_PROFILE, jobs=None, traffic_mode="functional"):
        """Cancel a caller-journaled token after restart; never create workers.

        Requires the same external ownership and Pod-UID fenced transport.
        A pre-ready orphan without complete child identities fails closed.
        Shard recovery requires the exact durable job manifest: rebuilding it
        with legacy defaults changes ports and can target the wrong identities.
        """
        if jobs is None:
            if profile == SHARD_PROFILE:
                raise PolicyBlocked('Shard recovery requires exact durable job manifest')
            jobs = build_jobs(bindings, profile=profile, traffic_mode=traffic_mode)
        else:
            try:
                jobs = tuple(job if isinstance(job, BatchJob) else BatchJob(**job) for job in jobs)
            except (TypeError, ValueError, KeyError) as exc:
                raise PolicyBlocked('Invalid durable batch job manifest') from exc
            if not jobs:
                raise PolicyBlocked('Invalid durable batch job manifest')
            rates = {}
            for job in jobs:
                directions = rates.setdefault(job.supi, {})
                if job.direction in directions:
                    raise PolicyBlocked('Duplicate durable directional job')
                directions[job.direction] = job.offered_kbps
            if any(set(v) != {'ul', 'dl'} for v in rates.values()):
                raise PolicyBlocked('Incomplete durable directional rates')
            if traffic_mode != 'qos':
                if any(v['ul'] != v['dl'] for v in rates.values()):
                    raise PolicyBlocked('Durable batch job rates changed')
                rates = {k: v['ul'] for k, v in rates.items()}
            if set(rates) != {row.get('supi') for row in bindings}:
                raise PolicyBlocked('Durable batch job population changed')
            shard_index = 0
            if profile == SHARD_PROFILE:
                minimum_port = min(job.port for job in jobs)
                shard_index = (minimum_port - 5200) // 100
            expected = build_jobs(
                bindings, aggregate_kbps=rate_budget(rates), offered_by_supi=rates,
                profile=profile, shard_index=shard_index, traffic_mode=traffic_mode)
            if jobs != expected:
                raise PolicyBlocked('Durable batch job manifest changed')
        _text(token, r'[a-f0-9]{32}', 'durable recovery token')
        identities = {(j.ue_pod, j.ue_uid) for j in jobs} | {(j.receiver_pod, j.receiver_uid) for j in jobs}
        if self._cancel_all(identities, token, profile=profile):
            raise BatchBlocked('batch.cleanup_unverified', 'Batch cleanup remains unverified')
        return {'token': token, 'cleanupVerified': True}

    def _wait_ready(self, groups, workers, run, role, deadline):
        """Parallel control probes; one unchanged absolute preparation budget.

        A separate fixed-size pool avoids starvation behind long worker futures.
        Drain in-flight probes before cleanup; a late READY is never accepted.
        The supplied run/execute retains each Pod's ownership and UID fences.
        """
        pending = set(groups)
        abort = Event()
        def probe(identity):
            if abort.is_set() or self.cancelled() or time.monotonic() >= deadline:
                raise BatchBlocked('batch.receiver_readiness_failed', 'Readiness cancelled or expired')
            group = groups[identity] if role == 'client_ready' else ()
            try:
                return run(identity, role, group, deadline=deadline).strip()
            except Exception:
                abort.set()  # Stop queued probes even before the owner drains futures.
                raise
        with ThreadPoolExecutor(max_workers=4) as pool:
            try:
                while pending and time.monotonic() < deadline:
                    futures = {identity: pool.submit(probe, identity) for identity in sorted(pending)}
                    for identity, future in futures.items():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise BatchBlocked('batch.receiver_readiness_failed', 'Readiness deadline exceeded')
                        try:
                            state = future.result(timeout=remaining)
                        except TimeoutError as exc:
                            raise BatchBlocked('batch.receiver_readiness_failed', 'Readiness deadline exceeded') from exc
                        if self.cancelled() or time.monotonic() >= deadline:
                            raise BatchBlocked('batch.receiver_readiness_failed', 'Readiness cancelled or expired')
                        if state == 'READY':
                            pending.remove(identity)
                        elif workers[identity].done():
                            # A worker that finishes before publishing readiness
                            # cannot succeed. Harvest its already-complete result
                            # only to retain a bounded local error category; never
                            # persist remote output or exception text.
                            try:
                                self._worker_output(
                                    workers[identity], identity,
                                    'client' if role == 'client_ready' else 'server',
                                    {j.key for j in groups[identity]}, deadline)
                            except Exception as worker_exc:
                                exc = BatchBlocked(
                                    'batch.receiver_readiness_failed',
                                    'Worker failed before readiness')
                                diagnostic = getattr(worker_exc, 'worker_diagnostic', None)
                                if isinstance(diagnostic, dict):
                                    exc.worker_diagnostic = diagnostic
                                transport = getattr(worker_exc, 'transport_diagnostic', None)
                                if isinstance(transport, dict):
                                    exc.transport_diagnostic = transport
                                raise exc from worker_exc
                            raise BatchBlocked('batch.receiver_readiness_failed',
                                               'Worker exited before readiness')
                        elif state != 'PENDING':
                            raise BatchBlocked('batch.receiver_readiness_failed', 'Worker failed before readiness')
                    if pending:
                        self.sleep(min(0.1, max(0, deadline-time.monotonic())))
                if pending:
                    raise BatchBlocked('batch.receiver_readiness_failed', 'Readiness deadline exceeded')
            finally:
                abort.set()

    def sample(self, bindings, *, token, reserve, aggregate_kbps=10_000, offered_by_supi=None,
               profile=LEGACY_PROFILE, shard_index=0, traffic_mode="functional"):
        if self.operation_observer is not None:
            if self._operation_used:
                raise PolicyBlocked('Observed batch operation cannot be replayed')
            self._operation_used = True
        jobs = build_jobs(bindings, aggregate_kbps=aggregate_kbps, offered_by_supi=offered_by_supi,
                          profile=profile, shard_index=shard_index, traffic_mode=traffic_mode)
        measurement_duration = (SHARD_DURATION_SECONDS if profile == SHARD_PROFILE
                                else 10 if profile == COMMISSIONING_PROFILE else 5)
        extra_duration = measurement_duration - 5
        if extra_duration and not all(callable(value) for value in (self.diagnostic_reporter,self.failure_reporter)):
            raise PolicyBlocked('Commissioning requires explicit durable receipt and failure reporters')
        _text(token, r'[a-f0-9]{32}', 'durable worker token')
        if not callable(reserve) or reserve(token, jobs) is not True:
            raise PolicyBlocked('Exact batch token/job intent must be durably reserved before execution')
        servers, clients = {}, {}
        for job in jobs:
            servers.setdefault((job.receiver_pod, job.receiver_uid), []).append(job)
            clients.setdefault((job.ue_pod, job.ue_uid), []).append(job)
        records, cleanup_errors = {}, []
        spans = _BatchSpans(self.monotonic)
        phase = 'native_preflight'
        def checkpoint():
            # Unvalidated diagnostics, NOT effect evidence. Original worker
            # times/JSON survive finally even when freshness reduction rejects.
            now = self.clock()
            diagnostics = {'observerNow': now if math.isfinite(now) else None}
            diagnostics['phaseSpans'] = spans.snapshot()
            for name in ('startedAt', 'finishedAt', 'receiverStartedAt', 'receiverFinishedAt'):
                values = [r[name] for r in records.values() if type(r.get(name)) in (int, float) and math.isfinite(r[name])]
                diagnostics[name + 'Min'] = min(values) if values else None
                diagnostics[name + 'Max'] = max(values) if values else None
            if self.diagnostic_reporter is not None and self.diagnostic_reporter(token, phase, records, diagnostics) is not True:
                raise BatchBlocked('batch.failure_persistence_failed', 'Batch diagnostic receipt could not be persisted')
        def report_failure(exc):
            try:
                checkpoint()
            except Exception:
                # Keep the causal failure; persistence failure is additional
                # fail-closed diagnostic state, never a reason to skip cleanup.
                exc.diagnostic_persistence_failed = True
            code = getattr(exc, 'code', 'batch.worker_failed')
            if code not in BATCH_ERROR_CODES:
                code = 'batch.worker_failed'
            exits = [{k: row[k] for k in ('clientExit', 'serverExit')
                      if type(row.get(k)) is int} | {'key': key} for key, row in records.items()]
            diagnostic = getattr(exc, 'transport_diagnostic', None)
            if isinstance(diagnostic, dict):
                exits.append(diagnostic)
            worker_diagnostic = getattr(exc, 'worker_diagnostic', None)
            if isinstance(worker_diagnostic, dict):
                exits.append(worker_diagnostic)
            try:
                if self.failure_reporter is not None and self.failure_reporter(token, phase, code, exits) is not True:
                    exc.diagnostic_persistence_failed = True
            except Exception:
                exc.diagnostic_persistence_failed = True
        def operation(event, code=None):
            if self.operation_observer is not None and self.operation_observer(
                    event, token, jobs, self.clock(), self.monotonic(), code) is not True:
                raise BatchBlocked('batch.failure_persistence_failed', 'Operation lifecycle unavailable')

        # Only this synchronous branch can certify that no executor or execute
        # call ever existed. A missing/failed start is NOT inferred from empty
        # raw records. The observer must durably finish before any work path.
        operation('started')
        try:
            if self.fence() is not True or self.native_guard(jobs) is not True:
                exc = BatchBlocked('batch.native_binding_changed', 'Fresh complete native binding required')
                if self.operation_observer is None:
                    report_failure(exc)  # Preserve legacy diagnostics exactly.
                raise exc
        except BaseException as exc:
            if self.operation_observer is not None:
                code = getattr(exc, 'code', 'batch.worker_failed')
                if type(code) is not str or code not in BATCH_ERROR_CODES:
                    code = 'batch.worker_failed'
                try:
                    operation('ended_no_dispatch', code)
                except BaseException:
                    try: exc.diagnostic_persistence_failed = True
                    except BaseException: pass
            raise  # Never substitute a persistence failure for this exception.

        # A recorded 10-second 50-UE run kept the oldest receiver wrapper alive
        # for 31.04 seconds. The 15-second profile therefore gets a fixed
        # 44-second host admission bound and 52-second transport bound. These
        # deadlines are created once and cannot be renewed by later evidence.
        sharded = profile == SHARD_PROFILE
        transport_timeout = 52 if sharded else 38
        transport_deadline = time.monotonic() + transport_timeout
        # The 18-second shard profile reserves 18 seconds for the bounded
        # 10-second receiver and 8-second client readiness phases. It then
        # retains enough of this fixed admission for common release, traffic,
        # and child collection. No observation renews the cap.
        # Admission below derives release slack from a common epoch, actual
        # Profile duration and two-second tail, without extending this cap.
        child_deadline = time.monotonic() + (SHARD_ADMISSION_TIMEOUT_SECONDS if sharded else 30)
        def run(identity, role, group=(), epoch=0, deadline=None):
            if self.fence() is not True:
                raise PolicyBlocked('Batch ownership fence rejected operation')
            remaining = min(transport_deadline, deadline if deadline is not None else transport_deadline) - time.monotonic()
            if remaining <= 0:
                raise BatchBlocked('batch.receiver_readiness_failed', 'Batch stage deadline expired')
            return self.execute(*identity, worker_command(role, token, group, epoch=epoch,profile=profile),
                                min(transport_timeout, remaining))

        operation('dispatch_possible')
        executor = ThreadPoolExecutor(max_workers=len(servers)+len(clients))
        cleanup_pool = cleanup_future = None
        cleanup_done = False
        causal_error = None

        def cleanup():
            # Exactly this token; both sweeps and the real worker join remain
            # one indivisible lifecycle, even if an observer fails.
            def observe_cleanup(event):
                if self.cleanup_observer is not None:
                    try:
                        self.cleanup_observer(event, self.clock())
                    except Exception:
                        cleanup_errors.append('cleanup_observer_failed')
            observe_cleanup('sweep1_begin')
            try:
                with spans.measure('cancel_sweep1'):
                    cleanup_errors.extend(self._cancel_all(set(servers) | set(clients), token, profile=profile))
            except Exception:
                cleanup_errors.append('sweep1_failed')
            observe_cleanup('sweep1_end')
            try:
                with spans.measure('worker_join'):
                    executor.shutdown(wait=True, cancel_futures=True)
                observe_cleanup('workers_joined')
            except Exception:
                cleanup_errors.append('workers_join_failed')
            observe_cleanup('sweep2_begin')
            try:
                with spans.measure('cancel_sweep2'):
                    cleanup_errors.extend(self._cancel_all(set(servers) | set(clients), token, profile=profile))
            except Exception:
                cleanup_errors.append('sweep2_failed')
            observe_cleanup('sweep2_end')

        def timed_cleanup():
            with spans.measure('cleanup'):
                cleanup()

        def finish_cleanup():
            nonlocal cleanup_done
            if cleanup_done:
                return
            try:
                if cleanup_future is None:
                    timed_cleanup()
                else:
                    try:
                        cleanup_future.result()
                    except Exception:
                        cleanup_errors.append('cleanup_future_failed')
            finally:
                if cleanup_pool is not None:
                    try:
                        cleanup_pool.shutdown(wait=True, cancel_futures=True)
                    except Exception:
                        cleanup_errors.append('cleanup_pool_join_failed')
                cleanup_done = True

        try:
            phase = 'receiver_prepare'
            server_futures = {identity: executor.submit(run, identity, 'server', group)
                              for identity, group in servers.items()}
            # Poll only owned readiness files, never connect to one-shot listeners.
            # Balanced shards reduce client workers to ten flows, but all shard
            # receivers still share one N6 container. Give concurrent server
            # listener startup its bounded ten seconds while preserving the
            # client's separate bounded eight-second readiness phase.
            server_readiness = receiver_readiness_seconds(servers, profile)
            deadline = min(time.monotonic() + server_readiness,
                           child_deadline - 15-extra_duration)
            with spans.measure('receiver_readiness'):
                self._wait_ready(servers, server_futures, run, 'ready', deadline)
            phase = 'client_measurement'
            client_futures = {identity: executor.submit(run, identity, 'client', group)
                              for identity, group in clients.items()}
            # Readiness is exact-token + manifest + live PID/start identities;
            # children do not start measurement until the separate release.
            preparation_deadline = min(time.monotonic() + readiness_seconds(clients),
                                       child_deadline - 15-extra_duration)
            with spans.measure('client_readiness'):
                self._wait_ready(clients, client_futures, run, 'client_ready', preparation_deadline)
            mono_before = time.monotonic()
            scheduled_at = self.clock()
            mono_after = time.monotonic()
            schedule = self.release_barrier or release_schedule
            epoch, release_deadline = schedule(wall=scheduled_at,
                mono_before=mono_before, mono_after=mono_after,
                child_deadline=child_deadline, transport_deadline=transport_deadline,
                duration=measurement_duration)
            # Separate control executor: worker futures occupy their slots.
            with ThreadPoolExecutor(max_workers=len(clients)) as release_pool:
                released = list(release_pool.map(
                    lambda identity: run(identity, 'release', clients[identity], epoch, release_deadline).strip(), clients))
            if any(value != 'RELEASED' for value in released):
                raise BatchBlocked('batch.receiver_readiness_failed', 'Client release was not confirmed')
            for identity, future in client_futures.items():
                group = clients[identity]
                actual = self._worker_output(future, identity, 'client', {j.key for j in group}, transport_deadline)
                for job in group:
                    item = actual[job.key]
                    records[job.key] = dict(job=job, client=item['report'], clientExit=item['exit'],
                                            startedAt=item['startedAt'], finishedAt=item['finishedAt'])
            for identity, future in server_futures.items():
                actual = self._worker_output(future, identity, 'server', {j.key for j in servers[identity]}, transport_deadline)
                for key, item in actual.items():
                    records[key].update(server=item['report'], serverExit=item['exit'],
                                        receiverStartedAt=item['startedAt'], receiverFinishedAt=item['finishedAt'])
            phase = 'native_postflight'
            with spans.measure('immutable_checkpoint'):
                checkpoint()
            self._catch_up(records)
            if profile == LEGACY_PROFILE:
                # Every paired output is already collected and durably offered
                # to the diagnostic reporter. Exact traffic-file cancellation
                # cannot alter the independently observed UE/PDU identity.
                # V2 stays sequential: its observer changes journal budgets.
                cleanup_pool = ThreadPoolExecutor(max_workers=1)
                cleanup_future = cleanup_pool.submit(timed_cleanup)
            with spans.measure('native_postflight'):
                if self.fence() is not True or self.native_guard(jobs) is not True:
                    raise BatchBlocked('batch.native_binding_changed', 'Native binding changed during actual batch traffic')
            if cleanup_future is not None:
                finish_cleanup()
            if not cleanup_errors:
                phase = 'evidence_reduce'
                checkpoint()
                with spans.measure('initial_reduce'):
                    samples = reduce_evidence(
                        jobs, records, now=self.clock(), profile=profile,
                        maximum_age=(SHARD_EVIDENCE_MAX_AGE_SECONDS
                                     if profile == SHARD_PROFILE else 15))
        except Exception as exc:
            # Paired raw evidence is immutable before parallel cancellation;
            # earlier failures still record diagnostics before fallback cleanup.
            causal_error = exc
            report_failure(exc)
            raise
        finally:
            finish_cleanup()
            if causal_error is not None and cleanup_errors:
                # Preserve the original native/worker error AND closure doubt.
                causal_error.cleanup_errors = tuple(cleanup_errors)
        if cleanup_errors:
            phase = 'cleanup'
            exc = BatchBlocked('batch.cleanup_unverified', 'Batch cleanup remains unverified')
            report_failure(exc)
            raise exc
        try:
            phase = 'final_freshness'
            checkpoint()
            samples = reduce_evidence(
                jobs, records, now=self.clock(), profile=profile,
                maximum_age=(SHARD_POST_CLEANUP_MAX_AGE_SECONDS
                             if profile == SHARD_PROFILE else 15))
        except Exception as exc:
            report_failure(exc)
            raise
        return {'samples': samples, 'rawRecords': records, 'token': token,
                'observedAt': min(s['finishedAt'] for s in samples), 'cleanupVerified': True}
