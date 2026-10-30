"""Pure journal contract for the fixed offered-load profile, not QoS evidence.

No I/O or admission changes. Transient PDU identities remain the provider's
responsibility; this contract freezes only exact SUPI/scenario/slice/rates.
"""
import json
import math

from backend.onprem.traffic.planning import PROFILE_ID, PROBE_PROFILE_ID, QOS_PROFILE_ID, SERVICE_PROFILE_ID, QOS_MAX_KBPS, plan_workload, plan_probe_workload, rate_budget


SCHEMA = "cityverse.round-workload/v1"
BUDGET_KBPS = 10_000


class WorkloadContractError(ValueError):
    pass


def _equal(left, right):
    # JSON-type equality avoids accepting True in place of an integer rate.
    try:
        return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return False


def workload_metadata(owners, *, traffic_mode="functional"):
    """Return only safe, fixed-profile allocation metadata (including zero UE)."""
    plan = plan_workload(owners, budget_kbps=QOS_MAX_KBPS if traffic_mode == "qos" else BUDGET_KBPS,
                         traffic_mode=traffic_mode)
    metadata = _metadata(plan)
    if traffic_mode == "qos":
        metadata["trafficMode"] = "qos"
    return metadata


def _metadata(plan):
    return {
        "schemaVersion": SCHEMA,
        "profileId": plan.profile_id,
        "budgetKbpsPerDirection": plan.budget_kbps_per_direction,
        "perUe": [{"supi": row.supi, "scenario": row.scenario, "slice": row.slice,
                   "requestedKbps": row.requested_kbps, "appliedKbps": row.applied_kbps}
                  for row in plan.per_ue],
        "requestedUplinkKbps": plan.requested_ul_kbps,
        "requestedDownlinkKbps": plan.requested_dl_kbps,
        "requestedCombinedKbps": plan.requested_combined_kbps,
        "appliedUplinkKbps": plan.applied_ul_kbps,
        "appliedDownlinkKbps": plan.applied_dl_kbps,
        "appliedCombinedKbps": plan.applied_combined_kbps,
        "capReason": plan.cap_reason,
        "comparisonFingerprint": plan.comparison_fingerprint,
        "qosEffectClaimed": False,
    }


def probe_workload_metadata(owners, probe_kbps=100):
    """Pure, explicit diagnostic profile; this function grants no execution authority."""
    plan = plan_probe_workload(owners, probe_kbps=probe_kbps, budget_kbps=BUDGET_KBPS)
    return {**_metadata(plan), "workloadKind": "diagnostic_probe", "probeKbpsPerDirection": probe_kbps}


def workload_metadata_for_snapshot(snapshot):
    """Select a probe only when the actual published fixture claim owns this round."""
    traffic_mode = snapshot.get("trafficMode", "functional")
    if traffic_mode not in ("functional", "qos"):
        raise WorkloadContractError("Unknown round traffic mode")
    actions = [a for a in snapshot.get("actions", []) if a.get("kind") == "scenario_validation_track"]
    if traffic_mode == "qos" and actions:
        raise WorkloadContractError("QoS experiments cannot substitute a diagnostic validation track")
    constrained = [a for a in actions if isinstance(a.get("fixtureClaim"), dict)
                   and a["fixtureClaim"].get("track") == "constrained"]
    if not constrained:
        if "serviceRequirements" in snapshot:
            return service_workload_metadata(snapshot)
        return workload_metadata(snapshot["perUe"], traffic_mode=traffic_mode)
    if len(actions) != 1 or len(constrained) != 1 or constrained[0].get("accepted") is not True:
        raise WorkloadContractError("Diagnostic probe has conflicting or unaccepted fixture claims")
    claim = constrained[0]["fixtureClaim"]
    from backend.onprem.agents.contracts import _digest
    from backend.onprem.validation.normal import cohort_digest
    from backend.onprem.validation.constrained import probe_workload_contract_digest
    probe, cap = claim.get("probeKbpsPerDirection"), claim.get("fixtureCapKbps")
    if (set(claim) != {"schema", "purpose", "track", "requestId", "events", "configSha256",
                      "workloadContractDigest", "manifestDigest", "roundId", "generation", "fleetLeaseDigest",
                      "comparisonFingerprint", "cohortSha256", "profileId", "fixtureCapKbps", "probeKbpsPerDirection"}
            or claim.get("schema") != "cityverse.scenario-constrained-fixture/v1"
            or claim.get("purpose") != "controlled_qer_fixture" or claim.get("profileId") != PROBE_PROFILE_ID
            or snapshot.get("strategy") != "ai"
            or type(snapshot.get("allocation", {}).get("total")) is not int
            or not 1 <= snapshot['allocation']['total'] <= 100
            or len(snapshot.get('perUe', [])) != snapshot['allocation']['total']
            or claim.get("requestId") != snapshot.get("requestId") or claim.get("roundId") != snapshot.get("roundId")
            or type(claim.get("generation")) is not int or claim["generation"] != snapshot.get("generation")
            or claim.get("events") != snapshot.get("allocation", {}).get("events")
            or claim.get("fleetLeaseDigest") != _digest(snapshot.get("fleetLease"))
            or claim.get("cohortSha256") != cohort_digest(snapshot["perUe"])
            or type(cap) is not int or type(probe) is not int or not 1 <= cap < probe <= 100
            or claim.get("workloadContractDigest") != probe_workload_contract_digest(claim["events"], probe)):
        raise WorkloadContractError("Diagnostic probe does not match the exact owned fixture contract")
    metadata = probe_workload_metadata(snapshot["perUe"], probe)
    if claim.get("comparisonFingerprint") != metadata["comparisonFingerprint"]:
        raise WorkloadContractError("Diagnostic probe fingerprint differs from the fixture claim")
    return metadata


def applied_rates(metadata):
    if metadata.get("profileId") == SERVICE_PROFILE_ID:
        return {row["supi"]: {"ul": row["appliedUplinkKbps"], "dl": row["appliedDownlinkKbps"]}
                for row in metadata["perUe"]}
    return {row["supi"]: row["appliedKbps"] for row in metadata["perUe"]}


def protocol_options(metadata):
    """Only an explicit QoS workload opts into the higher compiler bound."""
    if metadata.get("profileId") in {QOS_PROFILE_ID, SERVICE_PROFILE_ID} and metadata.get("trafficMode") == "qos":
        return {"traffic_mode": "qos"}
    return {}


def service_workload_metadata(snapshot):
    from backend.onprem.scenarios.service_requirements import ServiceRequirementCompiler
    import hashlib
    contract = ServiceRequirementCompiler.from_snapshot(snapshot)
    original = workload_metadata(snapshot["perUe"], traffic_mode="qos")
    counts = {name: sum(row['scenario'] == name for row in original['perUe']) for name in contract['services']}
    if (any(row['scenario'] not in contract['services'] for row in original['perUe'])
            or any(counts[name] != service['ueCount'] for name, service in contract['services'].items())):
        raise WorkloadContractError('Service workload differs from the frozen UE allocation')
    rows = []
    for owner in original["perUe"]:
        service = contract["services"][owner["scenario"]]
        ul, dl = service["offeredUplinkKbpsPerUe"], service["offeredDownlinkKbpsPerUe"]
        rows.append({**{k: owner[k] for k in ("supi", "scenario", "slice")},
                     "requestedUplinkKbps": ul, "requestedDownlinkKbps": dl,
                     "appliedUplinkKbps": ul, "appliedDownlinkKbps": dl})
    ul, dl = (sum(row[key] for row in rows) for key in ("appliedUplinkKbps", "appliedDownlinkKbps"))
    metadata = {**original, "profileId": SERVICE_PROFILE_ID, "perUe": rows,
                "serviceContractRevision": contract["revision"],
                "requestedUplinkKbps": ul, "requestedDownlinkKbps": dl, "requestedCombinedKbps": ul + dl,
                "appliedUplinkKbps": ul, "appliedDownlinkKbps": dl, "appliedCombinedKbps": ul + dl,
                "capReason": None}
    canonical = {k: metadata[k] for k in ("profileId", "perUe", "serviceContractRevision", "budgetKbpsPerDirection")}
    metadata["comparisonFingerprint"] = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return metadata


def _windows(snapshot):
    return [a for a in snapshot.get("actions", [])
            if a.get("kind") == "traffic_window" and a.get("mode") == "batch"]


def _candidate_100_shards(snapshot):
    """Return the exact partition for any 100-UE Pod topology."""
    rows = snapshot.get("perUe", [])
    if len(rows) != 100:
        return None
    from backend.onprem.traffic.planning import shard_supis
    try:
        return shard_supis(rows)
    except (ValueError, AttributeError) as exc:
        raise WorkloadContractError(str(exc)) from None


def _expected_shard_count(snapshot, population):
    if population == 100:
        return len(_candidate_100_shards(snapshot))
    count = min(10, (population + 9) // 10)
    proof = snapshot.get('typedCohortProof') or {}
    if (snapshot.get('schemaVersion') == 'cityverse.current-state/v2'
            and proof.get('schema') == 'cityverse.typed-measurement-cohort/v1'
            and proof.get('deviceCount') == proof.get('sessionCount') == population
            and population >= 10):
        return max(2, count)
    return count


def plan_for_stage(snapshot, *, current_only=False):
    """Freeze at baseline and demand exactly that plan at result measurement."""
    if snapshot.get("controlOnly") is True:
        raise WorkloadContractError("A control projection cannot authorize a new measurement plan")
    stage = snapshot.get("stage")
    if stage not in {"measuring_baseline", "measuring_result"}:
        raise WorkloadContractError("Workload planning requires a measurement stage")
    metadata = workload_metadata_for_snapshot(snapshot)
    if len(metadata["perUe"]) == 100:
        _candidate_100_shards(snapshot)
    history = _windows(snapshot)
    if any(a.get('stage') not in {'measuring_baseline','measuring_result'} for a in history):
        raise WorkloadContractError('Unknown traffic window stage')
    windows = [a for a in history if a.get('stage') == 'measuring_baseline']
    if (current_only and stage == 'measuring_result' and not windows
            and history and all(a.get('stage') == 'measuring_result' for a in history)):
        # The latest completed result carries the same frozen workload contract.
        # Validate its entire current cohort; never fall back to archived receipts.
        _validate_result_history(snapshot, metadata)
        return metadata
    if stage == "measuring_baseline":
        if history:
            raise WorkloadContractError("Baseline workload intent already exists")
    else:
        aggregate = [a for a in snapshot.get('actions', []) if a.get('kind') == 'traffic_aggregate'
                     and a.get('stage') == 'measuring_baseline']
        if aggregate:
            if (len(aggregate) != 1 or any(a.get('stage') != 'measuring_baseline' for a in windows)
                    or any(a.get(k) is not True for k in ('accepted', 'cleanupVerified', 'effectVerified')
                           for a in windows)
                    or aggregate[0].get('shardActionIds') != [a.get('id') for a in windows]
                    or aggregate[0].get('flowCount') != 2 * len(metadata['perUe'])
                    or aggregate[0].get('source') != 'native-iperf3-receiver'
                    or any(aggregate[0].get(k) is not True for k in
                           ('accepted', 'cleanupVerified', 'effectVerified'))
                    or any(type(aggregate[0].get(k)) not in (int, float)
                           or not math.isfinite(aggregate[0][k]) for k in
                           ('effectiveStartedAt', 'effectiveFinishedAt'))
                    or aggregate[0]['effectiveFinishedAt'] - aggregate[0]['effectiveStartedAt'] < 8
                    or len(windows) != _expected_shard_count(snapshot, len(metadata['perUe']))):
                raise WorkloadContractError('Incomplete baseline shard aggregation')
            ids = [row.get('supi') for window in windows for row in window.get('bindings', [])]
            if (len(ids) != len(set(ids)) or set(ids) != {row['supi'] for row in metadata['perUe']}
                    or [a.get('shardIndex') for a in windows] != list(range(len(windows)))
                    or any(a.get('shardCount') != len(windows) for a in windows)):
                raise WorkloadContractError('Baseline shard population differs from round')
            for window in windows:
                validate_record_workload(window, snapshot, allow_legacy=False)
        else:
            if (len(windows) != 1 or windows[0].get("stage") != "measuring_baseline"
                    or any(windows[0].get(k) is not True for k in ('cleanupVerified','accepted','installed','effectVerified'))):
                raise WorkloadContractError("Result workload requires one completed baseline intent")
            validate_record_workload(windows[0], snapshot, allow_legacy=False)
            if not _equal(windows[0].get("workload"), metadata):
                raise WorkloadContractError("Result workload differs from the frozen baseline")
        _validate_result_history(snapshot, metadata)
    return metadata


def _validate_result_history(snapshot, metadata):
    """A new AI cycle may follow complete results, never an unfinished window."""
    windows = [a for a in _windows(snapshot) if a.get('stage') == 'measuring_result']
    aggregates = [a for a in snapshot.get('actions', []) if a.get('kind') == 'traffic_aggregate'
                  and a.get('stage') == 'measuring_result']
    by_id = {a.get('id'): a for a in windows}
    if len(by_id) != len(windows) or any(a.get(k) is not True for a in windows
            for k in ('accepted','installed','effectVerified','cleanupVerified')):
        raise WorkloadContractError('Previous result window is incomplete or duplicated')
    for window in windows:
        validate_record_workload(window, snapshot, allow_legacy=False)
    sharded = any(a.get('shardIndex') is not None for a in windows)
    if not sharded:
        if aggregates:
            raise WorkloadContractError('Result aggregate has no owned shards')
        return
    if any(a.get('shardIndex') is None for a in windows):
        raise WorkloadContractError('Result history mixes measurement profiles')
    consumed = []
    population = {row['supi'] for row in metadata['perUe']}
    count = _expected_shard_count(snapshot, len(population))
    for aggregate in aggregates:
        ids = aggregate.get('shardActionIds')
        if (not isinstance(ids, list) or len(ids) != count or len(set(ids)) != len(ids)
                or any(identifier not in by_id or identifier in consumed for identifier in ids)
                or aggregate.get('flowCount') != 2 * len(population)
                or aggregate.get('source') != 'native-iperf3-receiver'
                or any(aggregate.get(k) is not True for k in ('accepted','effectVerified','cleanupVerified'))
                or any(type(aggregate.get(k)) not in (int,float) or not math.isfinite(aggregate[k])
                       for k in ('effectiveStartedAt','effectiveFinishedAt'))
                or aggregate['effectiveFinishedAt'] - aggregate['effectiveStartedAt'] < 8):
            raise WorkloadContractError('Previous result shard aggregation is incomplete')
        selected = [by_id[identifier] for identifier in ids]
        supis = [row.get('supi') for a in selected for row in a.get('bindings', [])]
        if ([a.get('shardIndex') for a in selected] != list(range(count))
                or any(a.get('shardCount') != count for a in selected)
                or len(supis) != len(population) or set(supis) != population):
            raise WorkloadContractError('Previous result shard ownership differs from the frozen population')
        consumed.extend(ids)
    if consumed != [a.get('id') for a in windows]:
        raise WorkloadContractError('Previous result contains orphaned or reordered shards')


def validate_record_workload(record, snapshot, *, allow_legacy):
    """Validate before replay or recovery; legacy means cleanup only, not upgrade.

    Returning None permits callers to drain an old exact-token/job reservation;
    it does not permit launching it or manufacture a missing historical plan.
    """
    if "workload" not in record:
        if allow_legacy:
            return None
        raise WorkloadContractError("Durable workload metadata is missing")
    if (snapshot.get("controlOnly") is True and isinstance(record["workload"], dict)
            and record["workload"].get("profileId") == PROBE_PROFILE_ID):
        metadata = _control_probe_metadata(record, snapshot)
    else:
        metadata = workload_metadata_for_snapshot(snapshot)
    if not _equal(record["workload"], metadata):
        raise WorkloadContractError("Durable workload profile, assignment or rates changed")
    expected = applied_rates(metadata)
    if record.get('shardIndex') is not None:
        rows = record.get('bindings', [])
        selected = {row.get('supi'): row.get('slice') for row in rows if isinstance(row, dict)}
        population = {row['supi']: row['slice'] for row in metadata['perUe']}
        count, index = record.get('shardCount'), record.get('shardIndex')
        if (type(count) is not int or type(index) is not int or not 2 <= count <= 10
                or not 0 <= index < count or not 1 <= len(rows) <= 50
                or len(selected) != len(rows) or any(population.get(supi) != slice_name
                                                     for supi, slice_name in selected.items())
                or not _equal(record.get('offeredBySupi'), {supi: expected[supi] for supi in selected})
                or type(record.get('aggregateKbps')) is not int
                or record['aggregateKbps'] != rate_budget({supi: expected[supi] for supi in selected})):
            raise WorkloadContractError('Durable shard workload differs from round plan')
        candidate = _candidate_100_shards(snapshot)
        if candidate is not None and (count != len(candidate)
                                      or frozenset(selected) != candidate[index]):
            raise WorkloadContractError('Durable 100-UE shard pairing differs from candidate plan')
        for prior in _windows(snapshot):
            if 'workload' in prior and not _equal(prior['workload'], metadata):
                raise WorkloadContractError('Round contains conflicting workload plans')
        return metadata
    if (not _equal(record.get("offeredBySupi"), expected)
            or type(record.get("aggregateKbps")) is not int
            or record["aggregateKbps"] != rate_budget(expected)):
        raise WorkloadContractError("Durable worker rates differ from the workload plan")
    rows = record.get("bindings", [])
    if (len(rows) != len(expected) or len({r.get("supi") for r in rows}) != len(rows)
            or {r.get("supi"): r.get("slice") for r in rows}
            != {r["supi"]: r["slice"] for r in metadata["perUe"]}):
        raise WorkloadContractError("Durable native assignments differ from workload ownership")
    for prior in _windows(snapshot):
        if "workload" in prior and not _equal(prior["workload"], metadata):
            raise WorkloadContractError("Round contains conflicting workload plans")
    return metadata


def _control_probe_metadata(record, snapshot):
    """Replay frozen control metadata, without creating a plan or fixture authority.

    RoundLedger.batch_control already checks durable raw-reference bindings.
    The detached caller still validates the exact reservation/token/jobs before
    any child command. No native evidence is supplied by this projection.
    """
    windows = _windows(snapshot)
    identifier, token = record.get("id"), record.get("token")
    if (not isinstance(identifier, str) or not identifier or not isinstance(token, str)
            or len(token) != 32 or any(c not in "0123456789abcdef" for c in token)
            or not 1 <= len(snapshot.get("perUe", [])) <= 100
            or type(record["workload"].get("probeKbpsPerDirection")) is not int
            or record["workload"]["probeKbpsPerDirection"] != 100):
        raise WorkloadContractError("Frozen diagnostic control identity is invalid")
    matching = [a for a in windows if a.get("id") == identifier and a.get("token") == token]
    controls = ("jobs", "bindings", "offeredBySupi", "aggregateKbps", "workload", "stage",
                "shardIndex", "shardCount", "executionMode", "detachedReservation")
    if len(matching) != 1 or any(not _equal(matching[0].get(k), record.get(k)) for k in controls):
        raise WorkloadContractError("Diagnostic control differs from its durable owned reservation")
    metadata = probe_workload_metadata(snapshot["perUe"], 100)
    if not windows or any(not _equal(a.get("workload"), metadata) for a in windows):
        raise WorkloadContractError("Diagnostic control contains conflicting frozen plans")
    return metadata
