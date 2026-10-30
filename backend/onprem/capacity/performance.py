"""Pure post-processing for capacity-delivery-v1; no promotion or QoS claim.

Caller must separately establish native identity, historical freshness, UI and
release evidence AND declare these criteria before a prospective trial. Calling
this on old receipts is a post-hoc audit, never proof of that protocol's use.

Pinned iperf 3.21 accounting sources:
https://github.com/esnet/iperf/blob/3.21/src/iperf_udp.c#L100-L184
https://github.com/esnet/iperf/blob/3.21/src/iperf_api.c#L4006-L4037
https://github.com/esnet/iperf/blob/3.21/src/iperf_api.c#L4270-L4295
https://github.com/esnet/iperf/blob/3.21/src/cjson.c#L516-L555
https://github.com/esnet/iperf/blob/3.21/src/iperf_udp.c#L272-L322
https://github.com/esnet/iperf/blob/3.21/src/iperf_server_api.c#L248-L265
https://github.com/esnet/iperf/blob/3.21/src/iperf_server_api.c#L443-L465

Receiver packets is the highest sequence, not datagrams received. Reordered
packets decrement outstanding loss; duplicates are not separately identified.
For fixed 1200-byte datagrams: packets-lost <= bytes/1200 <= packets-lost+OOO.
Conservatively subtract ALL OOO arrivals before crediting unique delivery.
Loss percentage uses the same report's sender count when available, otherwise
its receiver sequence count. JSON double checks allow 1e-12 relative / 1e-9
absolute numerical error only (cJSON prints 15/17 significant digits), never a
percentage relaxation of performance thresholds.

Sender sequence increments before write/byte accounting; server end reporting
precedes worker join. Captured gen64 shows a one-datagram counter discrepancy,
consistent with that non-atomic accounting (exact live interleaving unproven).
For this pinned single-stream/no-omit/no-GSO profile only, accept a sequence
excess of zero or one, expose it, and cap unique delivery credit by the sender's
byte-accounted datagrams. Never repair raw counters or inflate offered bytes.
"""
from backend.onprem.traffic.planning import rate_budget
from backend.onprem.traffic.workload import protocol_options
from dataclasses import asdict, dataclass
from ipaddress import ip_address
import math
from types import SimpleNamespace

from backend.onprem.traffic.protocol import (build_jobs, build_shards, validate_socket_pair,
    SHARD_DURATION_SECONDS, detached_receiver_contract, validate_receiver_contract,
    detached_accounting_interval)
from backend.onprem.policy.errors import PolicyBlocked
from backend.onprem.evidence.batch_receipts import encoded, digest, same_json
from backend.onprem.traffic.capability import receiver_summary, sender_summary
from backend.onprem.traffic.workload import validate_record_workload
from backend.onprem.traffic.planning import shard_supis
from backend.onprem.traffic.protocol import (AccountingObservation, reduce_evidence, SHARD_PROFILE,
    SHARD_POST_CLEANUP_MAX_AGE_SECONDS)
from backend.onprem.runtime.ue import seconds as observation_seconds


CRITERIA_ID = "capacity-delivery-v1"
PAYLOAD_BYTES = 1200


def criteria_metadata():
    return {"criteriaId": CRITERIA_ID, "minimumDeliveredAppliedRatio": .80,
            "maximumReceiverLossPercent": 5.0, "offeredAppliedRatioRange": [.80, 1.20],
            "deliveryBasis": "raw-and-conservative-unique-minus-all-out-of-order",
            "requiredStages": ["measuring_baseline", "measuring_result"],
            "requiredDirections": ["ul", "dl"], "payloadBytes": PAYLOAD_BYTES,
            "iperfVersion": "iperf 3.21", "qosEffectClaimed": False,
            "counterAccountingVersion": "pinned-udp-counter-v2",
            "maximumSenderSequenceByteGapPackets": 1,
            "uniqueDeliveryCappedBySenderBytes": True}


@dataclass(frozen=True)
class FlowPerformance:
    stage: str
    supi: str
    direction: str
    applied_kbps: int
    offered_applied_ratio: float
    received_applied_ratio: float
    conservative_unique_applied_ratio: float
    reported_loss_percent: float
    computed_loss_percent: float
    received_datagrams: int
    out_of_order: int
    sender_accounting_gap_packets: int
    passed: bool


@dataclass(frozen=True)
class CapacityAudit:
    passed: bool
    flows: tuple[FlowPerformance, ...]
    errors: tuple[str, ...]
    criteria_id: str = CRITERIA_ID
    scope: str = "bounded-offered-load-capacity-evidence-only"
    protocol_predeclaration_verified: bool = False
    promotion_eligible: bool = False
    qos_effect_claimed: bool = False


def _integer(value, *, positive=False):
    if type(value) is not int or not int(positive) <= value <= 2**53 - 1:
        raise ValueError("Exact bounded integer counter required")
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Finite nonnegative measurement required")
    return value


def _equal_number(actual, expected):
    return math.isclose(_number(actual), expected, rel_tol=1e-12, abs_tol=1e-9)


def _flow(job, report, stage):
    if (report.get("source") != "native-iperf3-receiver"
            or type(report.get("durationSeconds")) is not int
            or report["durationSeconds"] != job.duration
            or job.duration not in (5, SHARD_DURATION_SECONDS)):
        raise ValueError("Exact native measurement profile required")
    client, server = report["client"], report["server"]
    validate_socket_pair(job, client, server)
    for raw in (client, server):
        test = raw["start"]["test_start"]
        expected = {"protocol": "UDP", "num_streams": 1, "blksize": PAYLOAD_BYTES, "omit": 0,
                    "duration": job.duration, "reverse": int(job.direction == "dl"), "bidir": 0, "gso": 0, "gro": 0}
        if raw["start"].get("version") != "iperf 3.21" or any(type(test.get(k)) is not type(v) or test[k] != v for k, v in expected.items()):
            raise ValueError("Unsupported pinned UDP profile")
    receiver_raw = server if job.direction == "ul" else client
    sender_raw = client if job.direction == "ul" else server
    # Require the unambiguous modern summaries; do not use historical end.sum.
    if not isinstance(receiver_raw["end"].get("sum_received"), dict):
        raise ValueError("Explicit receiver summary required")
    received, sent = receiver_summary(receiver_raw), sender_summary(sender_raw)
    for summary in (received, sent):
        _integer(summary["bytes"], positive=True)
        _integer(summary["packets"], positive=True)
        if not job.duration - 1 <= _number(summary["seconds"]) <= job.duration + 2:
            raise ValueError("Incomplete measurement duration")
        if not _equal_number(summary["bits_per_second"], summary["bytes"] * 8 / summary["seconds"]):
            raise ValueError("Rate and actual bytes/duration disagree")
    lost = _integer(received["lost_packets"])
    streams = receiver_raw["end"]["streams"]
    if not isinstance(streams, list) or len(streams) != 1 or streams[0]["udp"].get("sender") is not False:
        raise ValueError("One exact receiver UDP stream required")
    ooo = _integer(streams[0]["udp"]["out_of_order"])
    if received["bytes"] % PAYLOAD_BYTES or sent["bytes"] % PAYLOAD_BYTES:
        raise ValueError("Fixed-payload byte accounting mismatch")
    sent_datagrams = sent["bytes"] // PAYLOAD_BYTES
    sender_gap = sent["packets"] - sent_datagrams
    if sender_gap not in (0, 1):
        raise ValueError("Sender sequence/byte accounting exceeds bounded pinned-profile discrepancy")
    datagrams = received["bytes"] // PAYLOAD_BYTES
    base = received["packets"] - lost
    if (received["packets"] > sent["packets"] or not 0 <= ooo <= datagrams
            or not base <= datagrams <= base + ooo):
        raise ValueError("Receiver sequence/loss/reorder accounting mismatch")
    local_sender_count = _integer(receiver_raw["end"]["sum_sent"]["packets"])
    if local_sender_count and local_sender_count != sent["packets"]:
        raise ValueError("Receiver peer sender count differs from actual sender")
    denominator = local_sender_count or received["packets"]
    computed_loss = 100 * lost / denominator
    reported_loss = _number(received["lost_percent"])
    if not _equal_number(reported_loss, computed_loss):
        raise ValueError("Reported loss percentage contradicts pinned denominator")
    for field, value in (("receivedBytes", received["bytes"]), ("packets", received["packets"]), ("lostPackets", lost)):
        if type(report.get(field)) is not int or report[field] != value:
            raise ValueError("Public receiver counters differ from raw report")
    if (not _equal_number(report["receivedMbps"], received["bits_per_second"] / 1e6)
            or not _equal_number(report["offeredObservedMbps"], sent["bits_per_second"] / 1e6)
            or not _equal_number(report["offeredTargetMbps"], job.offered_kbps / 1000)):
        raise ValueError("Public receiver rates differ from raw evidence")
    applied_bps = job.offered_kbps * 1000
    offered_ratio = sent["bits_per_second"] / applied_bps
    received_ratio = received["bits_per_second"] / applied_bps
    unique_ratio = min(datagrams - ooo, sent_datagrams) * PAYLOAD_BYTES * 8 / received["seconds"] / applied_bps
    passed = (.80 <= offered_ratio <= 1.20 and received_ratio >= .80 and unique_ratio >= .80
              and reported_loss <= 5.0 and computed_loss <= 5.0)
    return FlowPerformance(stage, job.supi, job.direction, job.offered_kbps, offered_ratio,
                           received_ratio, unique_ratio, reported_loss, computed_loss, datagrams, ooo, sender_gap, passed)


def _individual_flows(window, snapshot, owners):
    """Audit the native per-flow receipt used when batch traffic is disabled.

    This path has no batch workload or per-window cleanup receipt. It checks
    the immutable round fence, exact owner/job/report pairing, and the same raw
    pinned iperf counters as the batch path. Round cleanup stays a separate
    lifecycle fact rather than a delivery criterion.
    """
    if any(window.get(key) is not True for key in ("accepted", "installed", "effectVerified")):
        raise ValueError("Unfinished individual window")
    jobs, reports = window.get("jobs"), window.get("reports")
    if not isinstance(jobs, list) or not isinstance(reports, list) or len(jobs) != 2 * len(owners) or len(reports) != len(jobs):
        raise ValueError("Incomplete individual job/report manifest")
    identities = [{key: row.get(key) for key in ("supi", "slice", "podUid", "sessionId", "ueIp", "tun", "upf")}
                  for row in snapshot["perUe"]]
    fence = {"roundId": snapshot["roundId"], "generation": snapshot["generation"],
             "fleetDigest": snapshot["fleetLease"]["fleetDigest"],
             "supis": sorted(owners), "identities": identities}
    if not isinstance(fence["fleetDigest"], str) or not fence["fleetDigest"]:
        raise ValueError("Missing immutable fleet lease")
    expected = {(supi, direction) for supi in owners for direction in ("ul", "dl")}
    keyed_jobs = {(j["binding"]["supi"], j["direction"]): j for j in jobs}
    keyed_reports = {(r["binding"]["supi"], r["direction"]): r for r in reports}
    if len(keyed_jobs) != len(jobs) or len(keyed_reports) != len(reports) or set(keyed_jobs) != expected or set(keyed_reports) != expected:
        raise ValueError("Missing or duplicate individual receiver direction")
    flows = []
    for key in sorted(expected):
        job, report = keyed_jobs[key], keyed_reports[key]
        owner = owners[key[0]]
        binding = {**owner, "ip": owner["ueIp"], "generation": snapshot["generation"]}
        if (job.get("binding") != binding or report.get("binding") != binding
                or job.get("roundFence") != fence or job.get("duration") != 5
                or any(report.get(field) != job.get(source) for field, source in
                       (("receiverPodUid", "receiver_uid"), ("endpoint", "endpoint"),
                        ("port", "port"), ("durationSeconds", "duration"),
                        ("offeredTargetMbps", "offered_mbps")))):
            raise ValueError("Individual job/native receiver identity differs")
        rate = job.get("offered_mbps")
        if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 1000 or rate * 1000 != int(rate * 1000):
            raise ValueError("Invalid individual offered rate")
        if type(job.get("port")) is not int or not 1 <= job["port"] <= 65535:
            raise ValueError("Invalid individual receiver port")
        peer = report["server"]["start"]["connected"][0]["remote_host"]
        ip_address(peer)  # The N6/NAT peer is observed, not an immutable job field.
        normalized = SimpleNamespace(supi=key[0], direction=key[1],
                                     offered_kbps=int(rate * 1000), ue_ip=owner["ueIp"],
                                     endpoint=job["endpoint"], port=job["port"], receiver_peer=peer,
                                     duration=5)
        flows.append(_flow(normalized, report, window["stage"]))
    return flows


def sharded_capacity_windows(snapshot, *, expected_ues=100):
    """The existing general profile always requires BOTH original stages."""
    return _partitioned_capacity_windows(snapshot, expected_ues=expected_ues,
        stages=('measuring_baseline', 'measuring_result'))


def scoped_result_capacity_windows(snapshot):
    """Distinct predeclared typedweb profile: exact result-only100 cohort."""
    return _partitioned_capacity_windows(snapshot, expected_ues=100, stages=('measuring_result',))


def _partitioned_capacity_windows(snapshot, *, expected_ues, stages):
    """Return exact compiler-partitioned windows for the requested population.

    The compiler is the source of truth for disjoint bounded shards, ports,
    and immutable native jobs. The durable aggregate must account for every
    shard and equal the common effective iperf interval, not a reported average.
    """
    owners = {row["supi"]: row for row in snapshot["perUe"]}
    if (type(expected_ues) is not int or not 1 <= expected_ues <= 100
            or len(owners) != expected_ues or len(snapshot["perUe"]) != expected_ues):
        raise ValueError("Exact requested UE population required")
    windows = [a for a in snapshot["actions"] if a.get("kind") == "traffic_window"]
    aggregates = [a for a in snapshot["actions"] if a.get("kind") == "traffic_aggregate"]
    shard_count = len(shard_supis(snapshot["perUe"]))
    if len(windows) != len(stages) * shard_count or len(aggregates) != len(stages):
        raise ValueError("Both complete compiler-partitioned stages required")
    result = {}
    used_ids, used_tokens = set(), set()
    for stage in stages:
        selected = [a for a in windows if a.get("stage") == stage]
        merged = [a for a in aggregates if a.get("stage") == stage]
        if len(selected) != shard_count or len(merged) != 1:
            raise ValueError("One exact compiler-partitioned aggregate per stage required")
        aggregate = merged[0]
        if ([a.get("shardIndex") for a in selected] != list(range(shard_count))
                or any(a.get("shardCount") != shard_count or a.get("mode") != "batch" for a in selected)
                or aggregate.get("shardActionIds") != [a.get("id") for a in selected]
                or aggregate.get("flowCount") != 2 * expected_ues
                or aggregate.get("source") != "native-iperf3-receiver"
                or any(aggregate.get(k) is not True for k in
                       ("accepted", "cleanupVerified", "effectVerified"))):
            raise ValueError("Incomplete or altered shard aggregation")
        bindings = [row for window in selected for row in window["bindings"]]
        rates = {row["supi"]: window["offeredBySupi"][row["supi"]]
                 for window in selected for row in window["bindings"]}
        if len(bindings) != expected_ues or len(rates) != expected_ues:
            raise ValueError("Missing or duplicated shard owner")
        expected = build_shards(bindings, round_id=snapshot["roundId"],
                                generation=snapshot["generation"],
                                aggregate_kbps=rate_budget(rates), offered_by_supi=rates,
                                **protocol_options(selected[0].get('workload', {})))
        if len(expected) != shard_count:
            raise ValueError("Unexpected shard plan")
        reports_by_flow = {}
        for index, (window, jobs) in enumerate(zip(selected, expected)):
            identifier, token = window.get("id"), window.get("token")
            if (not isinstance(identifier, str) or not identifier or identifier in used_ids
                    or not isinstance(token, str) or len(token) != 32 or token in used_tokens
                    or any(ch not in "0123456789abcdef" for ch in token)):
                raise ValueError("Duplicate or malformed shard identity")
            used_ids.add(identifier)
            used_tokens.add(token)
            validate_record_workload(window, snapshot, allow_legacy=False)
            if (any(window.get(k) is not True for k in
                    ("accepted", "installed", "effectVerified", "cleanupVerified"))
                    or window.get("jobs") != [asdict(job) for job in jobs]
                    or len(window.get("reports", [])) != len(jobs)):
                raise ValueError("Shard jobs or lifecycle incomplete")
            contract = None
            receiver_window_max = 52  # Existing attached receipt rule.
            if window.get('executionMode') is not None or window.get('detachedReservation') is not None:
                if window.get('executionMode') != 'detached-shard-v1':
                    raise ValueError('Invalid detached shard receiver contract mode')
                try:
                    contract = detached_receiver_contract(window.get('detachedReservation'), jobs,
                        round_id=snapshot['roundId'], generation=snapshot['generation'],
                        shard_index=index, shard_count=len(expected), token=token)
                    reservation = validate_receiver_contract(contract, jobs, round_id=snapshot['roundId'],
                        generation=snapshot['generation'], shard_index=index, shard_count=len(expected), token=token)
                    receiver_window_max = reservation['serverChildDeadline'] - reservation['createdMonotonic']
                except PolicyBlocked:
                    raise ValueError('Invalid detached shard receiver reservation') from None
            reconstructed_raw = {}
            clocked = any('accounting_timestamp' in r.get(side, {}).get('start', {})
                          for r in window['reports'] for side in ('client', 'server'))
            recomputed = {}
            if clocked:
                observation = aggregate.get('accountingObservation')
                if contract is None or not AccountingObservation.valid(observation, boot_id=reservation['clockBootId']):
                    raise ValueError('Missing exact accounting observation for independent verification')
                diagnostics = window.get('workerDiagnostics', {})
                raw = diagnostics.get('rawRecords')
                if not isinstance(raw, dict) or set(raw) != {j.key for j in jobs}:
                    raise ValueError('Missing original accounting raw provenance')
                points = [p for p in diagnostics.get('checkpoints', []) if p.get('phase') == 'final_freshness']
                if (not points or any(type(p.get('observerBoottime')) not in (int, float)
                        or not math.isfinite(p['observerBoottime'])
                        or not 0 <= observation['observerBoottime'] - p['observerBoottime'] < 25 for p in points)):
                    raise ValueError('Accounting observation precedes closure or exceeds its freshness bound')
                try:
                    actual = reduce_evidence(jobs, {j.key: {**raw[j.key], 'job': j} for j in jobs},
                        now=observation['observerNow'], observer_boottime=observation['observerBoottime'],
                        maximum_age=SHARD_POST_CLEANUP_MAX_AGE_SECONDS,
                        profile=SHARD_PROFILE, receiver_contract=contract)
                except (PolicyBlocked, KeyError, TypeError, ValueError) as exc:
                    raise ValueError('Original accounting evidence failed independent reduction') from exc
                recomputed = {sample['job'].key: sample for sample in actual}
            elif 'accountingObservation' in aggregate:
                raise ValueError('Legacy flow cannot carry a kernel accounting observation')
            for job, report in zip(jobs, window["reports"]):
                if ((contract is not None and report.get('receiverContractDigest') != contract.sha256)
                        or (contract is None and 'receiverContractDigest' in report)):
                    raise ValueError('Shard receiver contract digest differs from its reservation')
                key = (job.supi, job.direction)
                if key in reports_by_flow or report.get("direction") != job.direction:
                    raise ValueError("Duplicate or reordered shard flow")
                owner = owners[job.supi]
                if (job.generation != snapshot["generation"] or job.ue_ip != owner["ueIp"]
                        or job.ue_uid != owner["podUid"] or job.tun != owner["tun"]
                        or any(getattr(job, key) != owner[field] for key, field in
                               (("session_id", "sessionId"), ("pfcp_seid", "pfcpSeid"),
                                ("sm_context_ref", "smContextRef"), ("upf_epoch", "upfEpoch"),
                                ("upf_name", "upf"), ("dnn", "dnn"), ("sst", "sst"), ("sd", "sd")))
                        or report.get("binding") != {**owner, "ip": owner["ueIp"],
                                              "generation": snapshot["generation"]}
                        or report.get("receiverPodUid") != job.receiver_uid
                        or report.get("endpoint") != job.endpoint
                        or report.get("port") != job.port):
                    raise ValueError("Shard receiver native identity differs")
                if clocked:
                    if any(not same_json(report.get(field), value) for field, value in recomputed[job.key].items()
                           if field != 'job'):
                        raise ValueError('Published accounting flow differs from the original raw evidence and clock pair')
                    reports_by_flow[key] = (job, report)
                    continue
                clocks = tuple(report.get(k) for k in ("startedAt", "finishedAt",
                    "receiverStartedAt", "receiverFinishedAt", "effectiveStartedAt",
                    "effectiveFinishedAt"))
                if any(type(value) not in (int, float) or not math.isfinite(value) for value in clocks):
                    raise ValueError("Missing finite actual shard clocks")
                started, finished, receiver_started, receiver_finished, effective_started, effective_finished = clocks
                if (not 17 <= finished - started <= 52
                        or not 17 <= receiver_finished - receiver_started <= receiver_window_max
                        or receiver_started > started + 1
                        or not finished - 1 <= receiver_finished <= finished + 3):
                    raise ValueError("Shard worker and receiver clocks disagree")
                timestamp = report["client"]["start"]["timestamp"]
                seconds, millis = timestamp.get("timesecs"), timestamp.get("timemillisecs")
                if (type(seconds) is not int or type(millis) is not int
                        or millis // 1000 != seconds
                        or (contract is None and not started <= millis / 1000 <= finished)):
                    raise ValueError("Shard iperf start is outside worker")
                sent = sender_summary(report["client"] if job.direction == "ul" else report["server"])
                received = receiver_summary(report["server"] if job.direction == "ul" else report["client"])
                if contract is not None:
                    client_finished = report.get('clientFinishedAt')
                    if (type(client_finished) not in (int, float) or not math.isfinite(client_finished)
                            or not 17 <= client_finished - started <= 46
                            or finished != min(client_finished, receiver_finished)
                            or not client_finished - 1 <= receiver_finished <= client_finished + 3):
                        raise ValueError('Detached original client wrapper differs from published clocks')
                    try:
                        expected_start, expected_finish = detached_accounting_interval(job,
                            report['client'], report['server'], started, client_finished,
                            receiver_started, receiver_finished)
                    except PolicyBlocked:
                        raise ValueError('Invalid detached local accounting interval') from None
                else:
                    expected_start = max(millis / 1000, receiver_started)
                    expected_finish = min(finished, receiver_finished, millis / 1000 + sent['seconds'],
                                          millis / 1000 + received['seconds'])
                if (not 17 <= sent["seconds"] <= 20 or not 17 <= received["seconds"] <= 20
                        or effective_started != expected_start or effective_finished != expected_finish):
                    raise ValueError("Shard effective iperf interval differs from raw receiver")
                if contract is not None:
                    reconstructed_raw[job.key] = dict(client=report['client'], server=report['server'],
                        clientExit=0, serverExit=0, startedAt=started, finishedAt=client_finished,
                        receiverStartedAt=receiver_started, receiverFinishedAt=receiver_finished)
                reports_by_flow[key] = (job, report)
            if contract is not None and not clocked:
                diagnostics = window.get('workerDiagnostics')
                if not isinstance(diagnostics, dict):
                    raise ValueError('Missing immutable detached raw provenance')
                reference = diagnostics.get('rawRecordsRef')
                if 'rawRecords' in diagnostics:
                    if 'rawRecordsRef' in diagnostics or not same_json(diagnostics['rawRecords'], reconstructed_raw):
                        raise ValueError('Detached published reports differ from immutable raw evidence')
                elif reference != {'schema': 'cityverse.batch-raw/v1', 'sha256': digest(encoded(reconstructed_raw))}:
                    raise ValueError('Detached published reports differ from immutable raw digest')
        if set(reports_by_flow) != {(supi, direction) for supi in owners for direction in ("ul", "dl")}:
            raise ValueError("Missing shard flow")
        starts = [report["effectiveStartedAt"] for _, report in reports_by_flow.values()]
        finishes = [report["effectiveFinishedAt"] for _, report in reports_by_flow.values()]
        if (any(type(value) not in (int, float) or not math.isfinite(value)
                for value in starts + finishes)
                or aggregate.get("effectiveStartedAt") != max(starts)
                or aggregate.get("effectiveFinishedAt") != min(finishes)
                or min(finishes) - max(starts) < 8):
            raise ValueError("Shard effective windows do not overlap")
        published = aggregate.get("finishedAt")
        if isinstance(published, str):
            try:
                published = observation_seconds(published)
            except (ValueError, TypeError):
                raise ValueError('Invalid shard publication timestamp') from None
        if (type(published) not in (int, float) or not math.isfinite(published)
                or published < aggregate["effectiveFinishedAt"]
                or published - aggregate["effectiveFinishedAt"] >= 35
                or any(not 0 <= published - report["finishedAt"] < 35
                       or not 0 <= published - report["receiverFinishedAt"] < 35
                        for _, report in reports_by_flow.values()
                        if 'accounting_timestamp' not in report.get('client', {}).get('start', {}))):
            raise ValueError("Shard receipts were stale at publication")
        result[stage] = (selected, aggregate, reports_by_flow)
    return result


def audit_capacity(snapshot):
    """Check every UE/direction in BOTH stages, never pass an aggregate average.

    Malformed evidence fails closed with fixed diagnostics. This does not read a
    clock or authenticate provenance; historical freshness/ownership, native
    gating and cleanup remain separate mandatory checks.
    """
    flows, errors = [], []
    try:
        if snapshot.get("strategy") != "none" or not (1 <= len(snapshot["perUe"]) <= 50
                                                      or len(snapshot["perUe"]) == 100):
            raise ValueError("Only nonzero NONE calibration is supported")
        owners = {r["supi"]: r for r in snapshot["perUe"]}
        if len(owners) != len(snapshot["perUe"]) or _integer(snapshot["allocation"]["total"], positive=True) != len(owners):
            raise ValueError("Population mismatch")
        if len(owners) == 100:
            for stage, (selected, aggregate, reports) in sharded_capacity_windows(snapshot).items():
                for job, report in reports.values():
                    sample = _flow(job, report, stage)
                    flows.append(sample)
                    if not sample.passed:
                        errors.append(f"{stage}:{job.supi}:{job.direction}:capacity_threshold_failed")
            return CapacityAudit(not errors, tuple(flows), tuple(errors))
        windows = [a for a in snapshot["actions"] if a.get("kind") == "traffic_window"]
        if len(windows) != 2 or {w.get("stage") for w in windows} != {"measuring_baseline", "measuring_result"}:
            raise ValueError("Both exact measurement stages required")
        modes = {window.get("mode") for window in windows}
        if modes not in ({"batch"}, {None}):
            raise ValueError("Mixed or unsupported measurement modes")
        for window in windows:
            if modes == {None}:
                for sample in _individual_flows(window, snapshot, owners):
                    flows.append(sample)
                    if not sample.passed:
                        errors.append(f"{sample.stage}:{sample.supi}:{sample.direction}:capacity_threshold_failed")
                continue
            validate_record_workload(window, snapshot, allow_legacy=False)
            if any(window.get(k) is not True for k in ("accepted", "installed", "effectVerified", "cleanupVerified")):
                raise ValueError("Unfinished window")
            jobs = build_jobs(window["bindings"], aggregate_kbps=window["aggregateKbps"], offered_by_supi=window["offeredBySupi"],
                              **protocol_options(window.get('workload', {})))
            if window["jobs"] != [asdict(j) for j in jobs] or len(window["reports"]) != 2 * len(owners):
                raise ValueError("Exact complete job manifest required")
            reports = {(r["binding"]["supi"], r["direction"]): r for r in window["reports"]}
            if set(reports) != {(supi, direction) for supi in owners for direction in ("ul", "dl")}:
                raise ValueError("Missing or duplicate receiver direction")
            for job in jobs:
                report = reports[(job.supi, job.direction)]
                owner = owners[job.supi]
                if report["binding"] != {**owner, "ip": owner["ueIp"], "generation": snapshot["generation"]}:
                    raise ValueError("Receiver identity differs from round")
                if (job.generation != snapshot["generation"] or job.ue_ip != owner["ueIp"]
                        or job.ue_uid != owner["podUid"] or job.tun != owner["tun"]
                        or any(getattr(job, key) != owner[value] for key, value in
                            (("session_id", "sessionId"), ("pfcp_seid", "pfcpSeid"),
                             ("sm_context_ref", "smContextRef"), ("upf_epoch", "upfEpoch"),
                             ("upf_name", "upf"), ("dnn", "dnn"), ("sst", "sst"), ("sd", "sd")))
                        or any(report.get(key) != value for key, value in
                               (("receiverPodUid", job.receiver_uid), ("endpoint", job.endpoint), ("port", job.port)))):
                    raise ValueError("Job/native/receiver identity differs")
                sample = _flow(job, report, window["stage"])
                flows.append(sample)
                if not sample.passed:
                    errors.append(f"{window['stage']}:{job.key}:capacity_threshold_failed")
    except Exception:
        errors.append("capacity_evidence_invalid_or_incomplete")
    return CapacityAudit(not errors, tuple(flows), tuple(errors))
