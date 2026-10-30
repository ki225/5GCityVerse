"""Read-only gtp5g kernel state exporter for one UPF network namespace.

Wire ABI: free5gc/go-gtp5gnl v1.6.1 attr_pdr.go/attr_far.go and
gtp5g v0.9.14/v0.10.1/v0.10.2 src/genl/genl_{pdr,far}.c. No PFCP log inference or replay.
FAR emission: https://github.com/free5gc/gtp5g/blob/v0.10.1/src/genl/genl_far.c
This file intentionally uses only stdlib so it can be mounted as a sidecar.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import os
import re
import select
from pathlib import Path
import socket
import struct
import threading
import time
import uuid

logger = logging.getLogger(__name__)
_TIMING_ERROR_TYPES = frozenset({
    "ObservationError", "TimeoutError", "OSError", "ValueError", "KeyError", "TypeError",
})


def _timing_error_type(error):
    name = type(error).__name__
    if name in _TIMING_ERROR_TYPES:
        return name
    if isinstance(error, TimeoutError):
        return "TimeoutError"
    if isinstance(error, OSError):
        return "OSError"
    return "UnexpectedError"

SCHEMA = "cityverse.upf/v1"
CMD_GET_PDR, CMD_GET_FAR, CMD_GET_QER = 7, 8, 9
NLM_F_REQUEST, NLM_F_DUMP, NLM_F_DUMP_INTR = 1, 0x300, 0x10
NLMSG_ERROR, NLMSG_DONE, NLMSG_OVERRUN = 2, 3, 4
IFLA_LINKINFO, IFLA_INFO_KIND, NLA_F_NESTED = 18, 1, 0x8000


class ObservationError(RuntimeError):
    pass


class NativeReadTiming:
    """Request-local numeric detail, never native state or acceptance evidence."""
    _scope = ContextVar('upf_native_read_timings', default=None)

    @classmethod
    @contextmanager
    def capture(cls, target, phase):
        details = {}
        token = cls._scope.set(details)
        try:
            yield
        finally:
            cls._scope.reset(token)
            if details:
                target.setdefault('nativeReadTimings', {})[phase] = details

    @classmethod
    @contextmanager
    def measure(cls, name):
        details = cls._scope.get()
        if details is None:
            yield
            return
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = max(0., (time.monotonic() - started) * 1000)
            entry = details.setdefault(name, {'count': 0, 'durationMs': 0.})
            entry['count'] += 1
            entry['durationMs'] = round(entry['durationMs'] + elapsed, 3)


@contextmanager
def _snapshot_lock(lock, deadline):
    """Queued readers share their original budget; expired work cannot pile up."""
    acquired = lock.acquire(timeout=max(0., deadline - time.monotonic()))
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def utc():
    return datetime.now(timezone.utc).isoformat()


def aligned(length):
    return (length + 3) & ~3


def attributes(payload: bytes):
    """Decode Linux nlattr TLVs, including nested flag and u64 pad attrs."""
    result = defaultdict(list)
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < 4:
            raise ObservationError("Truncated netlink attribute header")
        size, kind = struct.unpack_from("=HH", payload, offset)
        if size < 4 or offset + size > len(payload) or offset + aligned(size) > len(payload):
            raise ObservationError("Invalid netlink attribute length")
        result[kind & 0x3fff].append(payload[offset + 4:offset + size])
        offset += aligned(size)
    return dict(result)


def single(attrs, key, default=None):
    values = attrs.get(key, [])
    if len(values) > 1:
        raise ObservationError("Duplicate singleton netlink attribute")
    return values[0] if values else default


def number(attrs, key, width, required=False):
    value = single(attrs, key)
    if value is None:
        if required:
            raise ObservationError("Required kernel rule field missing")
        return None
    if len(value) != width:
        raise ObservationError("Kernel rule integer width mismatch")
    return int.from_bytes(value, byteorder=os.sys.byteorder)


def ipv4(value):
    if value is None:
        return None
    if len(value) != 4:
        raise ObservationError("Kernel IPv4 address width mismatch")
    return str(ipaddress.IPv4Address(value))


def decode_pdr(payload):
    a = attributes(payload)
    if any(len(value) != 4 for value in a.get(10, [])):
        raise ObservationError("Invalid PDR QER identifier width")
    pdi = attributes(single(a, 5, b""))
    fteid = attributes(single(pdi, 2, b""))
    return {"id": number(a, 3, 2, True), "seid": number(a, 11, 8, True),
            "farId": number(a, 7, 4, True), "ip": ipv4(single(pdi, 1)),
            "qerIds": [int.from_bytes(value, os.sys.byteorder) for value in a.get(10, [])],
            "ulTeid": number(fteid, 1, 4), "upfAddress": ipv4(single(fteid, 2))}


def decode_far(payload):
    a = attributes(payload)
    param = attributes(single(a, 5, b""))
    creation = attributes(single(param, 1, b""))
    action = single(a, 4)
    # gtp5g supports old one-byte and current two-byte Apply Action encodings.
    if action is None or len(action) not in {1, 2}:
        raise ObservationError("Invalid FAR Apply Action")
    return {"id": number(a, 3, 4, True), "seid": number(a, 7, 8, True),
            "action": int.from_bytes(action, os.sys.byteorder),
            "dlTeid": number(creation, 2, 4), "gnbAddress": ipv4(single(creation, 3)),
            "description": number(creation, 1, 2), "port": number(creation, 4, 2)}


def decode_qer(payload):
    """Official gtp5g 0.9.14/0.10.1/0.10.2 QER ABI; bit rates are kilobits/sec.

    Readback is installation evidence, never a claim of packet enforcement.
    """
    a = attributes(payload)
    def rate(key):
        fields = attributes(single(a, key, b""))
        return {direction: (number(fields, high, 4, True) << 8) | number(fields, low, 1, True)
                for direction, high, low in (("ul", 1, 2), ("dl", 3, 4))}
    related = single(a, 12, b"")
    if len(related) % 2:
        raise ObservationError("Invalid QER related PDR array")
    return {"id": number(a, 3, 4, True), "seid": number(a, 13, 8, True),
            "gate": number(a, 4, 1, True), "qfi": number(a, 9, 1, True),
            "mbrKbps": rate(5), "gbrKbps": rate(6),
            "pdrIds": sorted(struct.unpack("=" + "H" * (len(related) // 2), related))}


def decode_messages(datagram: bytes, sequence: int, family: int, port_id: int, *, single_reply=False):
    offset, bodies, done = 0, [], False
    while offset < len(datagram):
        if len(datagram) - offset < 16:
            raise ObservationError("Truncated netlink message")
        size, kind, flags, seq, pid = struct.unpack_from("=IHHII", datagram, offset)
        if size < 16 or offset + size > len(datagram):
            raise ObservationError("Invalid netlink message length")
        if seq != sequence or pid not in {0, port_id}:
            raise ObservationError("Unexpected netlink sequence/port; replay rejected")
        if flags & NLM_F_DUMP_INTR or kind == NLMSG_OVERRUN:
            raise ObservationError("Kernel state dump was interrupted")
        if single_reply and (flags & 2 or kind != family or bodies):
            raise ObservationError("Unexpected multipart/control netlink reply")
        body = datagram[offset + 16:offset + size]
        if done:
            raise ObservationError("Unexpected message after netlink completion")
        if kind == NLMSG_ERROR:
            if len(body) < 4 or struct.unpack_from("=i", body)[0] != 0:
                raise ObservationError("Kernel rejected state query")
        elif kind == NLMSG_DONE:
            if body and (len(body) < 4 or struct.unpack_from("=i", body)[0] != 0):
                raise ObservationError("Kernel dump completion reported an error")
            done = True
        elif kind == family:
            bodies.append(body)
        else:
            raise ObservationError("Unexpected netlink message family")
        offset += aligned(size)
    return bodies, done


def _netlink_pending(sock):
    # A socket's Python timeout must not turn this into a three-second poll.
    readable, _, exceptional = select.select([sock], [], [sock], 0)
    return bool(readable or exceptional)


class Netlink:
    """Only GET family/GET links/GET PDR/GET FAR requests are constructed."""
    def __init__(self, protocol, factory=socket.socket):
        self.closed, self.awaiting = False, False
        self.guard = None
        self.deadline = None
        if not hasattr(socket, "AF_NETLINK"):
            raise ObservationError("Linux AF_NETLINK is required")
        with NativeReadTiming.measure('socketCreate'):
            self.sock = factory(socket.AF_NETLINK, socket.SOCK_RAW, protocol)
        try:
            with NativeReadTiming.measure('socketTimeout'):
                self.sock.settimeout(3)
            with NativeReadTiming.measure('socketBind'):
                self.sock.bind((0, 0))
            with NativeReadTiming.measure('socketGetName'):
                self.port_id = self.sock.getsockname()[0]
            with NativeReadTiming.measure('sequenceRead'):
                self.sequence = int.from_bytes(os.urandom(3), "little")
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            with NativeReadTiming.measure('socketClose'):
                self.sock.close()

    def check_clean(self):
        with NativeReadTiming.measure('pendingCheck'):
            if self.closed or self.awaiting or _netlink_pending(self.sock):
                raise ObservationError('Netlink port has pending or incomplete state')

    def request(self, kind, body, dump=True, response_kind=None):
        if self.guard is not None:
            self.guard()
        self.check_clean()
        if self.sequence >= 0xffffffff:
            raise ObservationError('Netlink sequence exhausted')
        self.sequence += 1
        flags = NLM_F_REQUEST | (NLM_F_DUMP if dump else 0)
        deadline = min(time.monotonic() + 4, self.deadline or float('inf'))
        self._timeout(deadline)
        self.awaiting = True
        wire = struct.pack("=IHHII", 16 + len(body), kind, flags, self.sequence, self.port_id) + body
        with NativeReadTiming.measure('send'):
            if self.sock.sendto(wire, (0, 0)) != len(wire):
                raise ObservationError('Incomplete netlink request send')
        result = []
        total_bytes = 0
        while time.monotonic() < deadline:
            self._timeout(deadline)
            with NativeReadTiming.measure('receive'):
                data, _, msg_flags, peer = self.sock.recvmsg(1024 * 1024)
            if peer[0] != 0 or msg_flags & socket.MSG_TRUNC:
                raise ObservationError("Truncated/non-kernel netlink reply")
            total_bytes += len(data)
            if total_bytes > 32 * 1024 * 1024:
                raise ObservationError("Kernel dump exceeded bounded size")
            with NativeReadTiming.measure('decode'):
                records, done = decode_messages(data, self.sequence, response_kind or kind, self.port_id,
                                                single_reply=not dump)
            result.extend(records)
            if done or (not dump and records):
                self.awaiting = False
                self.check_clean()
                self._timeout(deadline)
                return result
        raise ObservationError("Kernel dump did not complete")

    def _timeout(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ObservationError('Kernel observation exceeded freshness budget')
        self.sock.settimeout(min(3., remaining))

    def family(self):
        name = b"gtp5g\0"
        attr = struct.pack("=HH", len(name) + 4, 2) + name
        attr += b"\0" * (aligned(len(attr)) - len(attr))
        rows = self.request(16, struct.pack("=BBH", 3, 1, 0) + attr, dump=False)
        # Linux 5.15 ctrl_getfamily returns NEWFAMILY, nlctrl version 2.
        if len(rows) != 1 or len(rows[0]) < 4 or rows[0][:4] != b'\x01\x02\x00\x00':
            raise ObservationError("gtp5g generic-netlink family unavailable")
        attrs = attributes(rows[0][4:])
        if single(attrs, 2) != b'gtp5g\0':
            raise ObservationError('Unexpected generic-netlink family name')
        family = number(attrs, 1, 2, True)
        if family < 0x10:
            raise ObservationError('Invalid generic-netlink family identity')
        return family


class KernelReader:
    def __init__(self, interface="upfgtp"):
        self.interface = interface
        self._ports = {}
        self._scope = None
        self._deadline = None
        self._capture_thread = None
        self._interface_index = None
        self._closed = False

    def _read_scope(self):
        with NativeReadTiming.measure('scopeRead'):
            worker = os.readlink('/proc/thread-self/ns/net')
            leader = os.readlink('/proc/self/ns/net')
            if worker != leader:
                raise ObservationError('Worker and process network namespaces differ')
            version = Path('/sys/module/gtp5g/version').read_text().strip().removeprefix('v')
            if version not in {'0.9.14', '0.10.1', '0.10.2'}:
                raise ObservationError('Unsupported gtp5g module version')
            module = Path('/sys/module/gtp5g').stat()
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            return (os.getpid(), worker, leader, boot, version,
                    module.st_dev, module.st_ino, module.st_ctime_ns, self.interface)

    def _check_scope(self):
        if self._closed or self._deadline is None or time.monotonic() >= self._deadline:
            raise ObservationError('Kernel reader closed or observation budget expired')
        if self._capture_thread != threading.get_ident():
            raise ObservationError('Kernel capture belongs to another thread')
        scope = self._read_scope()
        if self._scope is None:
            self._scope = scope
        elif scope != self._scope:
            raise ObservationError('Kernel socket ownership or namespace changed')
        if time.monotonic() >= self._deadline:
            raise ObservationError('Kernel observation exceeded freshness budget')
        return scope

    def _discard(self):
        ports, self._ports = self._ports, {}
        self._scope = self._interface_index = None
        error = None
        for port in ports.values():
            try:
                port.close()
            except BaseException as exc:
                error = error or exc
        if error is not None:
            raise error

    @contextmanager
    def capture(self, deadline, *, persistent=True):
        if self._deadline is not None:
            raise ObservationError('Concurrent or nested kernel capture rejected')
        self._deadline = deadline
        self._capture_thread = threading.get_ident()
        try:
            self.verify_capture()
            yield
        except BaseException:
            self._discard()
            raise
        finally:
            self._deadline = None
            self._capture_thread = None
            if not persistent:
                self._discard()

    def _port(self, protocol):
        self._check_scope()
        if protocol not in self._ports:
            port = Netlink(protocol)
            self._ports[protocol] = port
            self._check_scope()  # bind can be slow or cross a namespace change
            port.guard = self._check_scope
        port = self._ports[protocol]
        port.deadline = self._deadline
        return port

    def verify_capture(self):
        self._check_scope()
        for port in self._ports.values():
            port.check_clean()
        self._check_scope()

    def close(self):
        self._closed = True
        self._discard()

    def qos_readback(self):
        """Read the module-wide switch; no configuration or write fallback."""
        with Path('/proc/gtp5g/qos').open('rb') as handle:
            raw = handle.read(65)
        if len(raw) > 64 or re.fullmatch(rb'QoS Enable: [01]\n?', raw) is None:
            raise ObservationError('Malformed gtp5g QoS readback')
        return raw.decode('ascii').rstrip('\n')

    def identity(self):
        """Dump replies omit interface IDs: require exactly one gtp5g device."""
        if self._deadline is None:
            with self.capture(time.monotonic() + 5, persistent=False):
                value = self.identity()
                self.verify_capture()
                return value
        try:
            with NativeReadTiming.measure('moduleVersionRead'):
                version = Path("/sys/module/gtp5g/version").read_text().strip().removeprefix("v")
        except OSError:
            raise ObservationError("gtp5g kernel module version is unavailable") from None
        # v0.10.2 changes the add-rule error/lock paths; the generic-netlink
        # PDR/FAR/QER dump emitters and attribute IDs are unchanged from v0.10.1.
        if version not in {"0.9.14", "0.10.1", "0.10.2"}:
            raise ObservationError("Unsupported gtp5g module version; expected 0.9.14, 0.10.1 or 0.10.2")
        route = self._port(0)  # NETLINK_ROUTE
        # Keep the complete gtp5g inventory: a point query would hide a
        # second device while PDR/FAR/QER dumps omit originating link IDs.
        # Linux rtnl_dump_ifinfo accepts IFLA_LINKINFO / IFLA_INFO_KIND.
        name = b'gtp5g\0'
        kind = struct.pack('=HH', 4 + len(name), IFLA_INFO_KIND) + name
        kind += b'\0' * (aligned(len(kind)) - len(kind))
        linkinfo = struct.pack('=HH', 4 + len(kind), IFLA_LINKINFO | NLA_F_NESTED) + kind
        body = struct.pack("=BBHiII", 0, 0, 0, 0, 0, 0) + linkinfo
        rows = route.request(18, body, response_kind=16)
        links = []
        with NativeReadTiming.measure('linkDecode'):
            for row in rows:
                if len(row) < 16:
                    raise ObservationError("Truncated rtnetlink link")
                a = attributes(row[16:])
                info = attributes(single(a, 18, b""))
                if single(info, 1, b"").rstrip(b"\0") != b"gtp5g":
                    continue
                links.append((single(a, 3, b"").rstrip(b"\0").decode(), struct.unpack_from("=i", row, 4)[0]))
        # Validate every returned link even when an older kernel ignores the
        # filter. Never assume a filtered single row proves namespace uniqueness.
        if len(links) != 1 or links[0][0] != self.interface:
            raise ObservationError("UPF namespace must contain exactly one expected gtp5g interface")
        if self._interface_index is not None and self._interface_index != links[0][1]:
            raise ObservationError('Kernel interface identity changed')
        self._interface_index = links[0][1]
        with NativeReadTiming.measure('bootIdRead'):
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        with NativeReadTiming.measure('netnsRead'):
            netns = os.readlink("/proc/self/ns/net")
        return {"interface": links[0][0], "interfaceIndex": links[0][1], "moduleVersion": version,
                "bootId": boot, "netns": netns}

    def dump(self):
        if self._deadline is None:
            with self.capture(time.monotonic() + 5, persistent=False):
                value = self.dump()
                self.verify_capture()
                return value
        genl = self._port(16)  # NETLINK_GENERIC
        family = genl.family()
        pdr = genl.request(family, struct.pack("=BBH", CMD_GET_PDR, 0, 0))
        far = genl.request(family, struct.pack("=BBH", CMD_GET_FAR, 0, 0))
        qer = genl.request(family, struct.pack("=BBH", CMD_GET_QER, 0, 0))
        if any(len(row) < 4 for row in pdr + far + qer):
            raise ObservationError("Truncated generic-netlink header")
        return {"pdrs": sorted((decode_pdr(row[4:]) for row in pdr), key=lambda item: (item["seid"], item["id"])),
                "fars": sorted((decode_far(row[4:]) for row in far), key=lambda item: (item["seid"], item["id"])),
                "qers": sorted((decode_qer(row[4:]) for row in qer), key=lambda item: (item["seid"], item["id"]))}


def join_rules(snapshot, upf, interface_index, observed_at):
    pdrs, fars = snapshot["pdrs"], snapshot["fars"]
    for rules in (pdrs, fars):
        ids = [(row["seid"], row["id"]) for row in rules]
        if len(ids) != len(set(ids)) or any(not row["seid"] for row in rules):
            raise ObservationError("Ambiguous or zero kernel PFCP session identity")
    far_by_id = {(row["seid"], row["id"]): row for row in fars}
    groups = defaultdict(list)
    for pdr in pdrs:
        groups[pdr["seid"]].append(pdr)
    for far in fars:
        groups.setdefault(far["seid"], [])
    sessions, residuals = [], []
    for seid, rules in groups.items():
        ips = {rule["ip"] for rule in rules if rule["ip"]}
        ul = [rule for rule in rules if rule["ulTeid"]]
        dl = [rule for rule in rules if not rule["ulTeid"] and rule["ip"]]
        reason = None
        if len(ips) != 1 or len(ul) != 1 or len(dl) != 1:
            reason = "incomplete_or_ambiguous_pdr_pair"
        else:
            uplink_far = far_by_id.get((seid, ul[0]["farId"]))
            downlink_far = far_by_id.get((seid, dl[0]["farId"]))
            # FORW only: DROP/BUFF/DUPL are not an established forwarding path.
            if not uplink_far or not downlink_far or any(far["action"] != 2 for far in (uplink_far, downlink_far)):
                reason = "missing_or_nonforwarding_far"
            elif (uplink_far["dlTeid"] is not None or not downlink_far["dlTeid"]
                  or not downlink_far["gnbAddress"] or downlink_far["gnbAddress"] == "0.0.0.0"
                  or downlink_far["port"] != 2152 or downlink_far["description"] != 0x100
                  or not ul[0]["upfAddress"] or ul[0]["upfAddress"] == "0.0.0.0"):
                reason = "incomplete_gtpu_forwarding_parameters"
            else:
                sessions.append({"upf": upf, "pfcpSeid": str(seid), "ip": next(iter(ips)),
                                 "uplinkTeid": ul[0]["ulTeid"], "downlinkTeid": downlink_far["dlTeid"],
                                 "upfAddress": ul[0]["upfAddress"], "gnbAddress": downlink_far["gnbAddress"],
                                 "interfaceIndex": interface_index, "observedAt": observed_at,
                                 "evidence": "kernel-installed-pdr-far"})
        if reason:
            residuals.append({"pfcpSeid": str(seid), "ip": next(iter(ips)) if len(ips) == 1 else None, "reason": reason})
    for field in ("ip", "uplinkTeid"):
        if any(count > 1 for count in Counter(row[field] for row in sessions).values()):
            raise ObservationError("Conflicting kernel UE address or uplink tunnel")
    return sessions, residuals


class UpfCollector:
    def __init__(self, upf, reader=None, max_snapshot_seconds=5, *, policy_evidence=False, pod_uid=None,
                 commissioned_upfs=()):
        legacy_upfs = {"upf-embb", "upf-urllc", "upf-mmtc", "upf-v2x"}
        commissioned_upfs = tuple(commissioned_upfs)
        if (len(commissioned_upfs) > 64 or len(set(commissioned_upfs)) != len(commissioned_upfs)
                or any(not isinstance(name, str) or not re.fullmatch(r'upf-[a-z0-9][a-z0-9.-]{0,58}', name)
                       for name in commissioned_upfs)):
            raise ValueError("Pin exact bounded commissioned UPF identities")
        if upf not in legacy_upfs | set(commissioned_upfs):
            raise ValueError("Unknown on-prem UPF identity")
        if upf not in legacy_upfs and (not isinstance(pod_uid, str) or not re.fullmatch(
                r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', pod_uid)):
            raise ValueError("Managed UPF identity requires canonical Downward API Pod UID")
        self.upf, self.reader = upf, reader or KernelReader()
        if type(policy_evidence) is not bool:
            raise ValueError("Policy evidence opt-in must be boolean")
        if policy_evidence and (not isinstance(pod_uid, str) or not re.fullmatch(
                r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', pod_uid)):
            raise ValueError("Policy evidence requires canonical Downward API Pod UID")
        self.policy_evidence, self.pod_uid = policy_evidence, pod_uid
        self.max_snapshot_seconds = max_snapshot_seconds
        self.instance = str(uuid.uuid4())
        self.lock = threading.Lock()
        self._closing = threading.Event()

    def collect(self):
        observation_started = time.monotonic()
        timings = {}
        result = None
        unexpected_type = None
        try:
            result = self._collect(timings, observation_started)
            return result
        except BaseException as error:
            unexpected_type = _timing_error_type(error)
            raise
        finally:
            # This is producer-local timing; it is not a backend observation ID.
            try:
                elapsed_ms = round((time.monotonic() - observation_started) * 1000)
                error_type = unexpected_type or timings.get("errorType")
                if error_type is not None or elapsed_ms >= 500:
                    row = {"wallTimeUnixMs": round(time.time() * 1000),
                           "upfRole": self.upf,
                           "status": ("error" if unexpected_type is not None else
                                      "available" if result and result["available"] else "unavailable"),
                           "elapsedMs": elapsed_ms,
                           "lockWaitMs": timings.get("lockWaitMs"),
                           "identityBeforeMs": timings.get("identityBeforeMs"),
                           "dump1Ms": timings.get("dump1Ms"),
                           "dump2Ms": timings.get("dump2Ms"),
                           "identityAfterMs": timings.get("identityAfterMs"),
                           "errorType": error_type,
                           "nativeReadTimings": timings.get("nativeReadTimings", {})}
                    logger.warning("upf_native_timing %s", json.dumps(row, sort_keys=True))
            except Exception:
                pass  # Diagnostic logging must never alter native evidence semantics.

    def _collect(self, timings, observation_started):
        result = {"schemaVersion": SCHEMA, "available": False, "upf": self.upf,
                  "source": "gtp5g-netlink", "observedAt": None, "epoch": None,
                  "sessions": [], "residuals": [], "pdrs": [], "qers": [], "errors": []}
        result['qosObservation'] = {'available': False, 'source': '/proc/gtp5g/qos',
            'scope': 'kernel-module', 'readback': None, 'enabled': None,
            'observedAt': None, 'epoch': None, 'errors': ['Kernel snapshot unavailable']}
        if self.policy_evidence:
            result['policyEvidence'] = {'available': False, 'podUid': self.pod_uid,
                'podUidSource': 'configured-downward-api-unattested', 'requiresKubernetesUidFence': True,
                'selectedPdrClaimed': False, 'observedAt': None, 'epoch': None, 'fars': []}
        deadline = observation_started + self.max_snapshot_seconds
        with _snapshot_lock(self.lock, deadline) as acquired:
            timings['lockWaitMs'] = round((time.monotonic() - observation_started) * 1000)
            if not acquired:
                timings['errorType'] = 'ObservationError'
                result['errors'] = ['Kernel snapshot collector is busy; observation budget expired']
                return result
            started = observation_started
            def timed(name, function):
                if time.monotonic() >= deadline:
                    raise ObservationError('Kernel observation exceeded freshness budget')
                phase_started = time.monotonic()
                try:
                    with NativeReadTiming.capture(timings, name):
                        value = function()
                    if time.monotonic() >= deadline:
                        raise ObservationError('Kernel observation exceeded freshness budget')
                    return value
                finally:
                    timings[name] = round((time.monotonic() - phase_started) * 1000)
            try:
                if self._closing.is_set():
                    raise ObservationError('Kernel snapshot collector is closed')
                capture = getattr(self.reader, 'capture', None)
                context = capture(deadline) if callable(capture) else nullcontext()
                with NativeReadTiming.capture(timings, 'captureControlMs'), context:
                    before = timed('identityBeforeMs', self.reader.identity)
                    qos_first, qos_error = self._observe_qos()
                    first = timed('dump1Ms', self.reader.dump)
                    second = timed('dump2Ms', self.reader.dump)
                    qos_second, qos_second_error = self._observe_qos()
                    after = timed('identityAfterMs', self.reader.identity)
                    if before != after or first != second:
                        raise ObservationError("Kernel rules or network namespace changed during observation")
                    if time.monotonic() - started > self.max_snapshot_seconds:
                        raise ObservationError("Kernel observation exceeded freshness budget")
                    observed = utc()
                    sessions, residuals = join_rules(second, self.upf, before["interfaceIndex"], observed)
                    qers = second.get("qers", [])
                    ids = [(row["seid"], row["id"]) for row in qers]
                    if len(ids) != len(set(ids)) or any(not row["seid"] for row in qers):
                        raise ObservationError("Ambiguous or zero kernel QER identity")
                    pdr_ids = {(row["seid"], row["id"]) for row in second["pdrs"]}
                    for row in qers:
                        if not row["pdrIds"] or any((row["seid"], pdr) not in pdr_ids for pdr in row["pdrIds"]):
                            residuals.append({"pfcpSeid": str(row["seid"]), "qerId": row["id"], "reason": "orphan_qer"})
                    candidate = dict(result, qosObservation=dict(result['qosObservation']))
                    if self.policy_evidence:
                        candidate['policyEvidence'] = dict(result['policyEvidence'])
                    candidate.update(available=True, observedAt=observed, sessions=sessions, residuals=residuals,
                                  pdrs=second["pdrs"],
                                  qers=[dict(row, pfcpSeid=str(row["seid"]), netns=before["netns"],
                                             interfaceIndex=before["interfaceIndex"], observedAt=observed) for row in qers],
                                  moduleVersion=before.get("moduleVersion"), netns=before["netns"],
                                  epoch=f"{before['bootId']}:{before['netns']}:{before['interfaceIndex']}:{self.instance}",
                                  interfaceIndex=before["interfaceIndex"])
                    qos_error = qos_error or qos_second_error
                    if not qos_error and qos_first != qos_second:
                        qos_error = 'gtp5g QoS readback changed during observation'
                    if qos_error:
                        candidate['qosObservation']['errors'] = [qos_error]
                    else:
                        candidate['qosObservation'].update(available=True, readback=qos_second,
                            enabled=qos_second == 'QoS Enable: 1', observedAt=observed,
                            epoch=candidate['epoch'], errors=[])
                    if self.policy_evidence:
                        # Exact references in one stable kernel snapshot, NOT proof
                        # that a packet matched a PDR. Extra-PDR ambiguity above is
                        # retained. The future consumer must independently fence
                        # this selfreported UID against live Kubernetes Pod UIDs.
                        candidate['policyEvidence'].update(available=True, observedAt=observed,
                            epoch=candidate['epoch'], netns=before['netns'], interfaceIndex=before['interfaceIndex'],
                            fars=[dict(row, pfcpSeid=str(row['seid']),
                                relatedPdrIds=sorted(pdr['id'] for pdr in second['pdrs']
                                    if pdr['seid'] == row['seid'] and pdr['farId'] == row['id']))
                                for row in second['fars']])
                    verify = getattr(self.reader, 'verify_capture', None)
                    if callable(verify):
                        verify()
                    if time.monotonic() >= deadline:
                        raise ObservationError('Kernel observation exceeded freshness budget')
                result = candidate  # publish only after the capture context exits successfully
            except (ObservationError, OSError, ValueError, KeyError, TypeError) as exc:
                timings['errorType'] = _timing_error_type(exc)
                result["errors"] = [str(exc) if isinstance(exc, ObservationError) else "Kernel observation failed"]
        return result

    def close(self):
        self._closing.set()
        timings = {}
        started = time.monotonic()
        try:
            with self.lock, NativeReadTiming.capture(timings, 'closeMs'):
                close = getattr(self.reader, 'close', None)
                if callable(close):
                    close()
        finally:
            try:
                logger.warning('upf_native_close_timing %s', json.dumps({
                    'elapsedMs': round((time.monotonic() - started) * 1000, 3),
                    'nativeReadTimings': timings.get('nativeReadTimings', {})}, sort_keys=True))
            except Exception:
                pass  # Timing output cannot alter resource cleanup semantics.


    def _observe_qos(self):
        reader = getattr(self.reader, 'qos_readback', None)
        if not callable(reader):
            return None, 'gtp5g QoS reader unavailable'
        try:
            value = reader()
            if value not in ('QoS Enable: 0', 'QoS Enable: 1'):
                raise ObservationError('Malformed gtp5g QoS readback')
            return value, None
        except (OSError, ObservationError, UnicodeError):
            return None, 'gtp5g QoS readback unavailable or malformed'


def handler_for(collector):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/v1/upf-state":
                self.send_error(404)
                return
            result = collector.collect()
            body = json.dumps(result).encode()
            self.send_response(200 if result["available"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass
    return Handler


def create_servers(collector, host, port, loopback=False):
    """Bind all requested sockets before serving; never widen to Multus NICs."""
    servers = []
    try:
        primary = ThreadingHTTPServer((host, port), handler_for(collector))
        servers.append(primary)
        if loopback and host not in {"127.0.0.1", "0.0.0.0"}:
            servers.append(ThreadingHTTPServer(("127.0.0.1", primary.server_port), handler_for(collector)))
        return servers
    except OSError:
        for server in servers:
            server.server_close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upf", required=True)
    parser.add_argument("--commissioned-upf", action="append", default=[],
                        help="Operator-pinned extra UPF name; also requires POD_UID and independent consumer UID fencing")
    parser.add_argument("--interface", default="upfgtp")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9081)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loopback-listener", action="store_true", help="Also bind IPv4 localhost for kubectl port-forward")
    parser.add_argument("--policy-evidence", action="store_true", help="Opt in to raw FAR evidence; requires POD_UID from Downward API and independent consumer UID fencing")
    args = parser.parse_args()
    collector = UpfCollector(args.upf, KernelReader(args.interface),
                             policy_evidence=args.policy_evidence, pod_uid=os.environ.get('POD_UID'),
                             commissioned_upfs=args.commissioned_upf)
    servers = []
    try:
        if args.once:
            result = collector.collect()
            print(json.dumps(result))
            raise SystemExit(0 if result["available"] else 2)
        servers = create_servers(collector, args.host, args.port, args.loopback_listener)
        for server in servers[1:]:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        servers[0].serve_forever()
    finally:
        try:
            for server in servers[1:]:
                server.shutdown()
            for server in servers:
                server.server_close()
        finally:
            collector.close()


if __name__ == "__main__":
    main()
