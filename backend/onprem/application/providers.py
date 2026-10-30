"""Composition of native K8s, observation, traffic and policy adapters."""
from __future__ import annotations
from backend.onprem.storage.current_state import current_reader, read_cleanup
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from backend.onprem.settings import PROJECT_ROOT
from backend.onprem.runtime.ue import EvidenceUnavailable, validate_counts, shards
from backend.onprem.runtime.kubeadm import TRAFFIC_IMAGE, validate_terminal_recovery_receipt
from backend.onprem.runtime.errors import LocalKubernetesError
from backend.onprem.scenarios.coordinator import NativeRoundCoordinator
from backend.onprem.runtime.round_resources import NativeRoundResources, native_receiver_pods
from backend.onprem.runtime.errors import RoundRuntimeBlocked
from backend.onprem.policy.safety import policy_inventory_clear, policy_quarantine_clear
from backend.onprem.traffic.capability import IperfCapabilityTraffic
from backend.onprem.evidence.collector import NativeCollector

class CapacityStartupBlocked(RoundRuntimeBlocked):
    """Stop backend startup when neither allowed capacity mode is configured."""

def policy_quarantine_registry_from_environment(environment):
    """Resolve the trusted startup-only quarantine registry location."""
    configured = environment.get("CITYVERSE_ONPREM_POLICY_QUARANTINE_REGISTRY")
    if configured is None:
        return PROJECT_ROOT / ".tmp" / "onprem-policy-quarantine.json"
    registry = Path(configured)
    parent = registry.parent
    invalid_parent = (parent.is_symlink() or not parent.is_dir()
                      or (os.name != "nt" and parent.stat().st_mode & 0o077 != 0))
    if (not registry.is_absolute() or registry.is_symlink() or invalid_parent
            or (registry.exists() and not registry.is_file())):
        raise RoundRuntimeBlocked(
            "Configured policy quarantine registry requires an absolute path in a private directory")
    return registry

def scenario_runtime_enabled():
    return os.environ.get("CITYVERSE_ONPREM_SCENARIO_ROUNDS_ENABLED", "false").lower() == "true"

def load_terminal_recovery_receipt(environ):
    """Optional operator startup file with explicit raw-file SHA256 pin. No HTTP input."""
    filename = environ.get('CITYVERSE_ONPREM_TERMINAL_RECOVERY_FILE')
    expected = environ.get('CITYVERSE_ONPREM_TERMINAL_RECOVERY_SHA256')
    if filename is None and expected is None:
        return None
    try:
        if (not isinstance(filename, str) or not Path(filename).is_absolute()
                or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)):
            raise ValueError('Invalid startup receipt configuration')
        with Path(filename).open('rb') as stream:
            raw = stream.read(16385)
        if len(raw) > 16384 or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Receipt pin mismatch')
        receipt = json.loads(raw)
        validate_terminal_recovery_receipt(receipt, now=datetime.now(timezone.utc).timestamp())
        return receipt
    except (OSError, ValueError, TypeError, EvidenceUnavailable):
        raise RoundRuntimeBlocked('Pinned operator terminal recovery receipt is unavailable or invalid') from None

def terminal_cleanup_authorizer(controller, ledger, *, recovery_receipt=None):
    """Internal durable failed-round authority; no HTTP grant or status rewrite."""
    def authorize(pod):
        denied = 'Failed-round terminal cleanup ownership is not proven'
        snapshot = read_cleanup(ledger)
        if (snapshot.get('stage') != 'cleaning' or snapshot.get('executionOutcome') not in {'failed', 'cancelled'}
                or not snapshot.get('roundId') or type(snapshot.get('generation')) is not int):
            raise EvidenceUnavailable(denied)
        generation = snapshot['generation']
        allocations = [a for a in snapshot.get('actions', []) if a.get('kind') == 'ue_allocation']
        stops = [a for a in snapshot.get('actions', []) if a.get('kind') == 'ue_stop']
        if (len(allocations) != 1 or allocations[0].get('accepted') is not True
                or allocations[0].get('generation') != generation or len(stops) != 1
                or stops[0].get('accepted') is not True or type(stops[0].get('generation')) is not int
                or stops[0]['generation'] <= generation):
            raise EvidenceUnavailable(denied)
        with controller.store.connect() as db:
            # Read both rows in one read transaction. Driver repeatedly calls
            # this grant under its existing generation reconciliation lock.
            db.execute('BEGIN')
            current = controller.store.decode(db.execute('SELECT * FROM generations ORDER BY id DESC LIMIT 1').fetchone())
            historical = controller.store.decode(db.execute('SELECT * FROM generations WHERE id=?', (generation,)).fetchone())
        if (not current or not historical or current['generation'] != stops[0]['generation']
                or current['allocation'] or validate_counts(current['desired']) != dict.fromkeys(('embb', 'urllc', 'mmtc', 'v2x'), 0)
                or allocations[0].get('counts') != historical['desired']):
            raise EvidenceUnavailable(denied)
        meta = pod.get('metadata', {})
        labels = meta.get('labels', {})
        try:
            supis = json.loads(meta.get('annotations', {}).get('cityverse.io/supis', 'null'))
        except (ValueError, TypeError):
            raise EvidenceUnavailable(denied) from None
        expected = {f'cv-ue-g{generation}-{index}': part
                    for index, part in enumerate(shards(historical['allocation'], controller.driver.batch_size))}
        part = expected.get(meta.get('name'))
        if (not part or labels.get('cityverse.io/generation') != str(generation)
                or labels.get('cityverse.io/managed-by') != 'onprem-ue-controller'
                or labels.get('cityverse.io/slice') != part['slice'] or supis != part['supis']
                or not isinstance(meta.get('uid'), str) or not meta['uid']):
            raise EvidenceUnavailable(denied)
        again = read_cleanup(ledger)
        keys = ('roundId', 'generation', 'stage', 'executionOutcome')
        if any(again.get(k) != snapshot.get(k) for k in keys):
            raise EvidenceUnavailable(denied)
        # Action receipts must not be swapped between the two ledger reads.
        if [a for a in again.get('actions', []) if a.get('kind') in {'ue_allocation', 'ue_stop'}] != [
                a for a in snapshot.get('actions', []) if a.get('kind') in {'ue_allocation', 'ue_stop'}]:
            raise EvidenceUnavailable(denied)
        grant = {'roundId': snapshot['roundId'], 'generation': generation, 'podUid': meta['uid'],
                 'supis': supis, 'stage': 'cleaning', 'executionOutcome': snapshot['executionOutcome']}
        if recovery_receipt is not None and all(recovery_receipt.get(k) == grant[k] for k in ('roundId', 'generation', 'podUid')):
            grant['preDispatchRecovery'] = deepcopy(recovery_receipt)
        return grant
    return authorize

def configured_daily_native_observer(context):
    """Wire the observation-only FRU reader; UERANSIM remains explicitly unavailable."""
    driver = context.ue_controller.driver
    if driver is None:
        return None
    from backend.onprem.evidence.daily_native_observation import CityDailyNativeObserver

    kubeconfig = os.environ.get("KUBECONFIG", "")
    ran_backend = getattr(driver, "backend", None)

    def kubectl(args, *, timeout):
        command = ["kubectl"]
        if kubeconfig:
            command += ["--kubeconfig", kubeconfig]
        command += ["--context", driver.context, *args]
        result = driver.runner(command, text=True, capture_output=True, timeout=timeout, check=False)
        stdout = result.stdout or ""
        if result.returncode != 0 or len(stdout.encode("utf-8")) > 16 * 1024 * 1024:
            raise LocalKubernetesError("Daily native kubectl read failed")
        return stdout

    placeholders = {name: f"unavailable-{name}" for name in ("embb", "urllc", "mmtc", "v2x")}
    amf_pod = "unavailable-amf"
    gnb_pods, upf_pods, n6_pods = dict(placeholders), dict(placeholders), dict(placeholders)
    def resolve_pods(*, timeout):
        inventory = json.loads(kubectl(["get", "pods", "-n", driver.namespace, "-o", "json"], timeout=timeout))

        def one(*, app=None, nf=None, role=None, runtime=None):
            from backend.onprem.evidence.pod_identity import select_unique_pod
            try:
                return select_unique_pod(inventory.get("items", []), app=app, nf=nf, role=role, runtime=runtime)["metadata"]["name"]
            except ValueError as exc:
                raise LocalKubernetesError("Daily native pod inventory is ambiguous or incomplete") from exc

        return {
            "amf": one(app="free5gc-amf", nf="amf"),
            "gnb": {name: one(app=f"gnb-{name}", role="ran", runtime="kubeadm") for name in placeholders},
            "upf": {name: one(app=f"upf-{name}", role="upf") for name in placeholders},
            "n6": {name: one(app=f"n6-{name}", role="upf") for name in placeholders},
        }
    return CityDailyNativeObserver(
        kubectl, lambda timeout: context.ue_controller.status(), driver.namespace, amf_pod,
        gnb_pods, {name: f"upf-{name}-evidence" for name in placeholders}, upf_pods, n6_pods,
        ran_backend=ran_backend,
        pod_resolver=resolve_pods if ran_backend == "free-ran-ue" else None,
    )

async def configured_round_coordinator(context):
    """Start only receipt-verified AI100 or explicit unverified commissioning."""
    from backend.onprem.policy.pcf_http import configured_pcf_query_reader
    pcf_query_reader = configured_pcf_query_reader(os.environ)
    from backend.onprem.traffic.transports.cri_read import configured_read_transport
    from backend.onprem.traffic.transports.cri_release import configured_release_transport
    from backend.onprem.traffic.transports.cri_postflight import configured_postflight_transport
    from backend.onprem.traffic.transports.manager import configured_manager_transport
    if not scenario_runtime_enabled():
        configured_manager_transport(os.environ, detached_enabled=False)
        configured_read_transport(os.environ, getattr(context.ue_controller, "driver", None),
                                  detached_enabled=False)
        configured_release_transport(os.environ, getattr(context.ue_controller, "driver", None),
                                     detached_enabled=False)
        configured_postflight_transport(os.environ, getattr(context.ue_controller, "driver", None),
                                        detached_enabled=False)
        return None
    from backend.onprem.capacity.admission import calibration_from_environment
    calibration = calibration_from_environment(os.environ)
    driver, adapter = context.ue_controller.driver, context.ue_controller.adapter
    if (os.environ.get("CITYVERSE_ONPREM_RUNTIME") != "kubeadm"
            or os.environ.get("CITYVERSE_ONPREM_EVIDENCE_MODE") != "native"
            or driver is None or not driver.traffic_enabled
            or not isinstance(adapter, NativeCollector) or adapter.driver is not driver
            or getattr(adapter.native, "reference_db", None) is None):
        raise RoundRuntimeBlocked("NONE scenarios require native durable evidence and traffic-enabled kubeadm mutation driver")
    if any(not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image) for image in (driver.image, driver.init_image, TRAFFIC_IMAGE)):
        raise RoundRuntimeBlocked("NONE scenarios require digest-pinned UE and traffic images")
    kubeconfig = Path(os.environ.get("KUBECONFIG", ""))
    if not kubeconfig.is_absolute() or not kubeconfig.is_file():
        raise RoundRuntimeBlocked("NONE scenarios require one explicit existing KUBECONFIG shared with the native driver")
    if not await asyncio.to_thread(context.runtime_policy_clear):
        from backend.onprem.validation.restore import restored_cleanup_boot_allowed
        inventory = Path(os.environ.get('CITYVERSE_ONPREM_POLICY_JOURNAL_INVENTORY', ''))
        recoverable = (inventory.is_absolute() and policy_inventory_clear(inventory)
            and await asyncio.to_thread(restored_cleanup_boot_allowed,
                context.settings.round_state_file, context.policy_quarantine_registry,
                os.environ.get('CITYVERSE_ONPREM_AI100_CONFIG_FILE'),
                os.environ.get('CITYVERSE_ONPREM_AI100_CONFIG_SHA256')))
        if not recoverable:
            raise RoundRuntimeBlocked("Configured policy inventory or quarantine is unresolved")
    pods = (await asyncio.to_thread(driver._read_kubectl, "get", "pods", "-o", "json"))["items"]
    native_receiver_pods(pods, driver.namespace)
    traffic = IperfCapabilityTraffic("kubectl", kubeconfig, driver.context, driver.namespace)
    from backend.onprem.policy.absence import PcfRoundAbsence
    from backend.onprem.runtime.owned_native_teardown_adapter import configured_owned_native_teardown_factory
    native_teardown_factory=configured_owned_native_teardown_factory(context,os.environ)
    resources = NativeRoundResources(context.ue_controller, traffic, policy_quiet=context.runtime_policy_clear,
        session_workload_compiler=getattr(context,'intent_session_workload_compiler',None),
        owned_native_teardown_factory=native_teardown_factory or getattr(context,'owned_native_teardown_factory',None),
        pcf_absence=PcfRoundAbsence(driver, getattr(context.round_ledger, 'control_snapshot', current_reader(context.round_ledger)),
                                    release_reader=context.round_ledger.released_snapshot_matches,
                                    generation_reader=context.ue_controller.store.read_generation,
                                    held_reader=getattr(context.round_ledger, 'intent_traffic_held', None),
                                    query_reader=pcf_query_reader),
        duration=5, concurrency=4)
    resources.subset_leasing_enabled = True
    from backend.onprem.traffic.batch import NativeRoundBatch
    from backend.onprem.traffic.tuning import tuning_from_environment, verify_host_socket_tuning
    batch_enabled = os.environ.get("CITYVERSE_ONPREM_BATCH_TRAFFIC_ENABLED", "false").lower() == "true"
    detached_enabled = os.environ.get("CITYVERSE_ONPREM_DETACHED_BATCH_ENABLED", "false").lower() == "true"
    if detached_enabled and (os.environ.get("CITYVERSE_ONPREM_PROFILE") != "wsl-kubeadm"
                             or not batch_enabled or not scenario_runtime_enabled()):
        raise RoundRuntimeBlocked("Detached batch requires WSL scenario and batch modes")
    detached_read_transport = configured_read_transport(
        os.environ, driver, detached_enabled=detached_enabled)
    detached_release_transport = configured_release_transport(
        os.environ, driver, detached_enabled=detached_enabled)
    detached_postflight_transport = configured_postflight_transport(
        os.environ, driver, detached_enabled=detached_enabled)
    manager_transport = configured_manager_transport(os.environ, detached_enabled=detached_enabled)
    if manager_transport is not None:
        from backend.onprem.evidence.kubernetes_pod_read import KubernetesPodRead
        from backend.onprem.traffic.transports.failure_capture import ManagerFailureCapture
        try:
            diagnostic_reader = await asyncio.to_thread(KubernetesPodRead,
                driver.context, driver.namespace, kubeconfig=kubeconfig, capacity=1)
            capture = ManagerFailureCapture(diagnostic_reader,
                context.settings.round_state_file.parent / 'manager-failure-captures.jsonl')
            context.manager_failure_capture = capture
            manager_transport.failure_capture = capture
        except Exception:
            # Optional diagnostic availability never changes admission authority.
            pass
    traffic_tuning = tuning_from_environment(os.environ)
    if batch_enabled and detached_enabled:
        # Fail before serving or admitting UE work when a reboot has reset the
        # kernel limits. Keep the per-window check for later setting changes.
        await asyncio.to_thread(verify_host_socket_tuning, traffic_tuning)
    typed_batch_flag = os.environ.get('CITYVERSE_ONPREM_V2_BATCH_ENABLED', 'false')
    if typed_batch_flag not in {'true', 'false'}:
        raise RoundRuntimeBlocked('V2 batch opt-in must be exactly true or false')
    batch_class = NativeRoundBatch
    if typed_batch_flag == 'true':
        if not (batch_enabled and detached_enabled) or getattr(driver, 'backend', None) != 'free-ran-ue':
            raise RoundRuntimeBlocked('Typed V2 batch requires commissioned FreeRAN detached batch transport')
        from backend.onprem.traffic.v2_batch import V2NativeRoundBatch
        batch_class = V2NativeRoundBatch
    resources.batch = batch_class(resources, context.round_ledger,
        enabled=batch_enabled, detached=detached_enabled,
        traffic_tuning=traffic_tuning, detached_read_transport=detached_read_transport,
        detached_release_transport=detached_release_transport,
        detached_postflight_transport=detached_postflight_transport,
        manager_transport=manager_transport)
    if calibration.enabled and not resources.batch.enabled:
        raise RoundRuntimeBlocked("Commissioning requires the fixed-profile native batch provider")
    from backend.onprem.agents.startup import UnverifiedAiTrial, load_adaptive_startup
    capacity_grant, capacity_grant_reader, adaptive_runtime = await asyncio.to_thread(
        load_adaptive_startup, os.environ, driver, TRAFFIC_IMAGE, context.round_ledger)
    ai_trial = type(capacity_grant) is UnverifiedAiTrial
    from backend.onprem.capacity.typed_web_receipts import configured_scoped_capacity
    from backend.onprem.intents.integration import load_configuration
    scoped_capacity = configured_scoped_capacity(load_configuration(), driver, context.round_ledger)
    if scoped_capacity is not None:
        # A configured manifest must verify before startup; trial-only captures
        # convey no capacity and still require existing commissioning admission.
        await asyncio.to_thread(scoped_capacity.read_grant)
    if capacity_grant is None and not calibration.enabled and scoped_capacity is None:
        raise CapacityStartupBlocked(
            "Scenario backend requires a verified capacity grant, explicit AI trial, or unverified commissioning")
    if capacity_grant is not None and calibration.enabled:
        raise CapacityStartupBlocked("Verified AI startup and unverified commissioning cannot be combined")
    if capacity_grant is not None and (not callable(capacity_grant_reader) or adaptive_runtime is None):
        raise CapacityStartupBlocked("100-UE AI startup requires a complete adaptive runtime")
    if adaptive_runtime is not None and not resources.batch.enabled:
        raise RoundRuntimeBlocked("100-UE AI requires the fixed-profile native batch provider")
    if adaptive_runtime is not None:
        # Private exact-owner exception for a controlled fixture only. Public
        # admission still uses runtime_policy_clear() with no exception.
        def fixture_policy_quiet(snapshot):
            inventory = Path(os.environ.get("CITYVERSE_ONPREM_POLICY_JOURNAL_INVENTORY",
                                            PROJECT_ROOT / ".tmp" / "onprem-policy-journal-inventory.json"))
            return (scenario_runtime_enabled() and inventory.is_absolute()
                    and policy_inventory_clear(inventory)
                    and adaptive_runtime.fixture_owned_quiet(snapshot, context.policy_quarantine_registry))
        resources.fixture_policy_quiet = fixture_policy_quiet
        def fixture_cleanup_quiet(snapshot):
            inventory = Path(os.environ.get("CITYVERSE_ONPREM_POLICY_JOURNAL_INVENTORY",
                                            PROJECT_ROOT / ".tmp" / "onprem-policy-journal-inventory.json"))
            return (scenario_runtime_enabled() and inventory.is_absolute()
                    and policy_inventory_clear(inventory)
                    and adaptive_runtime.fixture_restored_quiet(snapshot, context.policy_quarantine_registry))
        resources.fixture_cleanup_quiet = fixture_cleanup_quiet
    coordinator = NativeRoundCoordinator(
        context.round_ledger, resources,
        verified_max_ues=(1 if capacity_grant is None and scoped_capacity is not None and not calibration.enabled
                          else 0 if ai_trial or capacity_grant is None else capacity_grant.verified_max_ues),
        capacity_grant=capacity_grant, capacity_grant_reader=capacity_grant_reader,
        adaptive_runtime=adaptive_runtime, calibration=calibration,
        scoped_capacity=scoped_capacity,
        measurement_timings=context.measurement_timings,
        intent_traffic_restorer=getattr(context,'intent_traffic_restorer',None),
        intent_traffic_renewer=getattr(context,'intent_traffic_renewer',None),
        group_ai_reason=("100-UE adaptive AI unavailable: complete capacity receipts, "
                         "patched SMF QER actuator and native UPF readback "
                         "are not configured"))
    driver.terminal_authorizer = terminal_cleanup_authorizer(context.ue_controller, context.round_ledger,
        recovery_receipt=load_terminal_recovery_receipt(os.environ))
    return coordinator

