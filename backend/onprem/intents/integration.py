"""Composition root for the real city runtime; no synthetic telemetry fallback."""
from __future__ import annotations

from dataclasses import asdict
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import time

from .domain import ContractError, PolicyAuthority, ServiceIntent, SessionBinding, SessionKey, FlowBinding, timestamp, finite
from .observations import Metric, ObservationSnapshot, CapacityProfile, evaluate_objective
from .repository import IntentRepository
from .service import IntentService
from .membership import MembershipResolver
from backend.onprem.runtime.current_sessions import V2, IDENTITY_FIELDS, canonical_session_key, session_rows


def load_configuration(environ=None):
    env = os.environ if environ is None else environ
    name, digest = env.get('CITYVERSE_INTENT_CONFIG_FILE'), env.get('CITYVERSE_INTENT_CONFIG_SHA256')
    if not name and not digest:
        return {}
    if not name or not digest:
        raise ContractError('intent_configuration_requires_path_and_digest')
    path = Path(name)
    if not path.is_absolute() or path.is_symlink():
        raise ContractError('invalid_intent_configuration_path')
    with path.open('rb') as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576 or hashlib.sha256(raw).hexdigest() != digest:
        raise ContractError('intent_configuration_digest_mismatch')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('schema') != 'cityverse.intent-configuration/v1':
        raise ContractError('invalid_intent_configuration_schema')
    return value


class CurrentCityObserver:
    """Consume only the current accepted window, retaining its actual clocks and identity."""
    def __init__(self, context, config, *, clock=time.time):
        self.context, self.config, self.clock = context, config, clock
        self.maximum_age = float(config.get('maximum_observation_age_seconds', 30))
        # Preserve the effective formal-service default, and expose the same
        # operator value to the service's outer deadline and this inner wait.
        self.initial_observation_timeout_seconds=finite(
            config.get('initial_observation_timeout_seconds',120),'initial observation timeout',.001)
        self.resource_telemetry = getattr(context, 'intent_resource_telemetry', None)
        self.session_binding_reader = getattr(context, 'intent_session_binding_reader', None)
        self.nf_load_reader = getattr(context, 'intent_nf_load_reader', None)
        self.empty_population_reader = getattr(context, 'intent_empty_population_reader', None)

    def _planning_context(self, raw, intent, sessions):
        """Current native geography is evidence, never a static demand catalog."""
        from backend.onprem.agents.contracts import _digest
        spatial = raw.get('spatialDemand')
        result = {}
        if spatial:
            runtime = getattr(self.context, 'placement_service', None)
            repository = getattr(runtime, 'repository', None)
            reader = getattr(runtime, 'context_reader', None)
            if repository is not None and callable(reader):
                from .placement import CityTopology
                with repository.connect() as db:
                    owner = db.execute('SELECT owner FROM intents WHERE id=?', (intent.intent_id,)).fetchone()
                if owner is None:
                    raise ContractError('spatial_intent_owner_unknown')
                topology = runtime.read_topology()
                result = reader(intent.intent_id, owner['owner'], topology)
            else:
                # Raw native attachment is still useful to a QER-only commissioned
                # controller. No slots, capacity or placement actions are invented.
                result = {'source': 'current-native-scenario', 'round_id': raw.get('roundId'),
                    'generation': raw.get('generation'), 'spatial_demand': spatial,
                    'placement_status': 'not_configured', 'configured_actions': []}
        paths = ()
        path_revision = ''
        route_reader = getattr(self.context, 'intent_path_binding_reader', None)
        routes = getattr(self.context, 'intent_service_routes', None)
        descriptors = [self._route_session(s) for s in sessions]
        bound = ([r for r in descriptors if routes.has_session(raw, r)]
                 if callable(getattr(routes, 'has_session', None)) else [])
        if callable(route_reader) or bound and callable(getattr(routes, 'observe_bindings', None)):
            value = (route_reader(intent, tuple(sessions)) if callable(route_reader)
                     else routes.observe_bindings(raw, bound))
            if not isinstance(value, dict) or not value.get('source') or not isinstance(value.get('bindings'), list):
                raise ContractError('native_path_binding_evidence_invalid')
            if not 0 <= self.clock() - timestamp(value.get('observed_at')) <= self.maximum_age:
                raise ContractError('native_path_binding_evidence_stale')
            paths = tuple(value['bindings'])
            path_revision = value.get('revision')
            if not isinstance(path_revision, str) or not path_revision:
                raise ContractError('native_path_binding_revision_missing')
            if bound:
                from .evidence_contracts import PathEvidence
                windows = routes.receiver_windows(raw, bound)
                enriched = []
                for path in paths:
                    proofs = []
                    for window in windows:
                        route = window.get('serviceRoute') or {}
                        if route.get('session_key') != path.get('session_key'):
                            continue
                        if window.get('pathEvidence'):
                            proof = PathEvidence.from_dict(window['pathEvidence'])
                            if (proof.session_key != path['session_key'] or proof.application_pod_uid != path['application_pod_uid']
                                    or proof.endpoint_revision != path['endpoint_revision'] or proof.upf_epoch != path['upf_epoch']):
                                raise ContractError('receiver_path_native_identity_changed')
                            proofs.append(proof.to_dict())
                            continue
                        # Missing forwarding IDs or a producer flow identity is
                        # unknown evidence. Never synthesize those identities.
                        if not path.get('pdr_ids') or not path.get('far_ids') or not window.get('flow_id'):
                            continue
                        proof = PathEvidence(evidence_id=_digest([path, window]), intent_id=intent.intent_id,
                            operation_id=window.get('flow_operation_id', route['operation_id']), round_id=route['round_id'], generation=route['generation'],
                            session_key=path['session_key'], upf_target_id=path['upf_target_id'], upf_epoch=path['upf_epoch'],
                            pfcp_seid=str(path['pfcp_seid']), pdr_ids=tuple(path['pdr_ids']), far_ids=tuple(path['far_ids']),
                            application_instance_id=path['application_instance_id'], application_pod_uid=path['application_pod_uid'],
                            endpoint_revision=path['endpoint_revision'], flow_id=window['flow_id'], receiver_id=window['receiverPodUid'],
                            native_evidence_refs=(_digest(path), _digest(window)),
                            window_started_at=timestamp(window['startedAt']), observed_at=timestamp(window['finishedAt']))
                        proofs.append(proof.to_dict())
                    enriched.append(dict(path, receiver_path_evidence=proofs))
                paths = tuple(enriched)
        workload = raw.get('workload') or raw.get('resolvedRequirements')
        return dict(spatial_context=result, spatial_context_ref=_digest(result) if result else '',
            topology_revision=result.get('topology', {}).get('revision', spatial.get('topologyRevision') if spatial else None),
            demand_revision=_digest([raw.get('allocation', {}).get('demandGroups'), workload]) if spatial else '',
            cohort_digest=_digest(sorted(s.key.token for s in sessions)),
            path_bindings_ref=path_revision, path_evidence=paths,
            offered_load_ref=_digest(workload or {'traffic_mode': raw.get('trafficMode', 'functional'),
                'allocation': raw.get('allocation', {}), 'profile': 'scenario-offered-load-v1'}))

    @staticmethod
    def _route_session(session):
        return dict(sessionKey=session.key.token, supi=session.key.supi,
                    sessionId=session.key.pdu_session_id, smContextRef=session.key.established_epoch)

    def _application_reports(self, raw, session):
        routes = getattr(self.context, 'intent_service_routes', None)
        native = self._route_session(session)
        if not callable(getattr(routes, 'has_session', None)) or not routes.has_session(raw, native):
            return None
        reports = routes.receiver_windows(raw, (native,))
        if not reports:
            raise ContractError('current_application_receiver_measurements_missing')
        return reports

    def needs_reconcile(self, intent, snapshot):
        membership = MembershipResolver().resolve(intent, snapshot.sessions)
        tokens = tuple(sorted({m.session_token for m in membership.members}))
        flows = tuple(sorted({m.session_token + ':' + m.flow_id for m in membership.members if m.flow_id}))
        return bool(membership.rejected) or any(
            evaluate_objective(objective, snapshot, (intent.intent_id,) if objective.scope == 'group'
                else tokens if objective.scope == 'per_session' else flows, self.clock()).status != 'passed'
            for objective in intent.objectives)

    def bind_admissions(self, intent, prepared, batch):
        source = getattr(self.context, 'intent_observation_source', None)
        bind = getattr(source, 'bind_admissions', None)
        if callable(bind):
            return bind(intent, prepared, batch)
        # A native reader may already resolve the newly established identities.
        # The runtime subsequently requires those actual keys in the fresh cohort.
        if self.session_binding_reader is None:
            raise ContractError('native_admission_observer_not_configured')

    async def after_change(self, intent, not_before, deadline):
        source = getattr(self.context, 'intent_observation_source', None)
        measure = getattr(source, 'measure_after', None)
        if callable(measure):
            from .service import invoke
            await invoke(measure, intent, not_before, deadline)
        while self.clock() < min(deadline, intent.valid_until):
            from .service import invoke
            snapshot = await invoke(self.snapshot, intent)
            fresh = snapshot.fresh_metrics(self.clock())
            if fresh and all(m.observed_at - m.window_seconds >= not_before for m in fresh):
                return snapshot
            await asyncio.sleep(min(1, max(0, min(deadline, intent.valid_until) - self.clock())))
        raise ContractError('post_installation_measurement_deadline_exceeded')

    async def wait_for_current(self, intent, deadline, *, expected_request_id=None):
        transient = {'no_active_city_observation', 'current_receiver_measurements_missing', 'receiver_session_missing',
                     'current_application_receiver_measurements_missing'}
        deadline = min(deadline, intent.valid_until,
                       self.clock() + self.initial_observation_timeout_seconds)
        while self.clock() < deadline:
            from .service import invoke
            try:
                value = await invoke(self.snapshot, intent)
                if (not expected_request_id or value.traffic_request_id == expected_request_id) and (not value.sessions or value.fresh_metrics(self.clock())):
                    return value
            except ContractError as error:
                if str(error) not in transient:
                    raise
            await asyncio.sleep(min(1, max(0, deadline - self.clock())))
        raise ContractError('initial_observation_deadline_exceeded')

    def snapshot(self, intent):
        source = getattr(self.context, 'intent_observation_source', None)
        if source is not None:
            value = source.snapshot(intent)
            return value if isinstance(value, ObservationSnapshot) else ObservationSnapshot.from_dict(value)
        ledger = self.context.round_ledger
        control = ledger.batch_control()
        if not control.get('roundId') or control.get('stage') == 'completed':
            controller = getattr(self.context, 'ue_controller', None)
            state = controller.status() if controller is not None else {}
            if (state.get('status') == 'stopped' and state.get('evidenceSources') and not state.get('errors')
                    and all(type(state.get(k)) is int and state[k] == 0
                            for k in ('requested', 'allocated', 'registered', 'pduEstablished', 'trafficVerified', 'failed'))):
                at = timestamp(state['observedAt'])
                if 0 <= self.clock() - at <= self.maximum_age:
                    version = f"fleet-zero:{state['generation']}"
                    return ObservationSnapshot(f'{version}:{at}', at, version,
                        maximum_age_seconds=self.maximum_age, traffic_request_id=control.get('requestId', ''))
            raise ContractError('no_active_city_observation')
        # live_snapshot selects the current window; current()/archive APIs are intentionally not used.
        raw = ledger.live_snapshot(control['roundId'])
        return self.from_native(raw, intent)

    def _empty_population(self, raw):
        """A current native absence proof is independent of receiver windows."""
        marker = raw['nativeEmptyPopulation']
        if (raw.get('devices') != [] or raw.get('perSession') != []
                or not isinstance(marker, dict) or not callable(self.empty_population_reader)):
            raise ContractError('native_empty_population_proof_unavailable')
        try:
            proof = self.empty_population_reader(raw)
            identity = ('source', 'generation', 'populationRevision', 'operationId', 'deltaId', 'deltaVersion')
            if (not isinstance(proof, dict) or proof.get('source') != 'native-owned-empty-population/v1'
                    or proof.get('roundId') != raw.get('roundId')
                    or any(proof.get(k) != marker.get(k) for k in identity)
                    or any(type(proof.get(k)) is not int for k in ('generation', 'populationRevision'))
                    or proof['generation'] != raw.get('generation')
                    or proof['populationRevision'] != raw.get('populationRevision')
                    or any(not isinstance(proof.get(k), str) or not proof[k]
                           for k in ('operationId', 'deltaId', 'deltaVersion'))):
                raise ValueError('Empty population identity changed')
            tombstones = proof.get('tombstones')
            if (not isinstance(tombstones, list) or not 1 <= len(tombstones) <= 100
                    or tombstones != marker.get('tombstones')):
                raise ValueError('Empty population removal identity changed')
            fields = {'deviceId', 'supi', 'podIndex', 'nativeAbsent', 'podAbsent'}
            for row in tombstones:
                if (not isinstance(row, dict) or set(row) != fields
                        or any(not isinstance(row[k], str) or not row[k] for k in ('deviceId', 'supi'))
                        or type(row['podIndex']) is not int or row['podIndex'] < 0
                        or row['nativeAbsent'] is not True or row['podAbsent'] is not True):
                    raise ValueError('Empty population absence not proven')
            if len({r['deviceId'] for r in tombstones}) != len(tombstones):
                raise ValueError('Duplicate removed device')
            observed_at = timestamp(proof['observedAt'])
            version = hashlib.sha256(json.dumps([raw.get('roundId'), *[proof[k] for k in identity],
                tombstones], sort_keys=True, allow_nan=False).encode()).hexdigest()
        except Exception:
            raise ContractError('native_empty_population_proof_unavailable') from None
        if not 0 <= self.clock() - observed_at <= self.maximum_age:
            raise ContractError('observation_stale')
        return ObservationSnapshot('owned-empty:' + version + ':' + str(observed_at), observed_at,
            'owned-empty:' + version, maximum_age_seconds=self.maximum_age,
            traffic_request_id=raw.get('requestId', ''))

    def from_native(self, raw, intent):
        owners, measurements = session_rows(raw), raw.get('measurements', ())
        v2 = raw.get('schemaVersion') == V2
        if v2 and not owners and raw.get('nativeEmptyPopulation') is not None:
            return self._empty_population(raw)
        if owners and raw.get('nativeEmptyPopulation') is not None:
            raise ContractError('empty_population_marker_conflicts_with_sessions')
        if not owners or not measurements:
            raise ContractError('current_receiver_measurements_missing')
        mapped = self.config.get('session_bindings', ())
        sessions, metrics, times, native_paths = [], [], [], []
        reader = self.session_binding_reader
        batch_read = getattr(reader, 'current_bindings', None)
        try:
            resolved = batch_read(raw) if callable(batch_read) else None
        except Exception:
            raise ContractError('native_session_inventory_binding_unavailable') from None
        measured = {}
        for row in measurements:
            identity = canonical_session_key(row.get('sessionKey')).token if v2 else (row.get('supi'), row.get('sessionId'))
            if identity in measured:
                raise ContractError('ambiguous_receiver_session')
            measured[identity] = row
        if v2 and set(measured) != {r['sessionKey'] for r in owners}:
            raise ContractError('receiver_device_session_coverage_changed')
        for owner in owners:
            row = measured.get(owner['sessionKey'] if v2 else (owner.get('supi'), owner.get('sessionId')))
            if row is None or row.get('source') != 'receiver':
                raise ContractError('receiver_session_missing')
            for field in ('roundId', 'generation'):
                if row.get(field) != raw.get(field):
                    raise ContractError('foreign_receiver_observation')
            if not owner.get('upfEpoch') or row.get('upfEpoch') != owner.get('upfEpoch'):
                raise ContractError('receiver_path_epoch_changed')
            if v2 and any(row.get(k) != owner.get(k) for k in IDENTITY_FIELDS):
                raise ContractError('receiver_device_session_identity_changed')
            if reader is not None:
                try:
                    proof = (resolved[(owner.get('supi'), str(owner.get('sessionId')))]
                             if resolved is not None else reader(owner, raw))
                    if (proof.get('source') != 'native-smf-session-inventory'
                            or type(proof.get('observed_at')) not in (int, float)
                            or not 0 <= self.clock() - proof['observed_at'] <= self.maximum_age
                            or proof.get('native_supi') != owner.get('supi')
                            or str(proof.get('native_session_id')) != str(owner.get('sessionId'))
                            or proof.get('native_generation') != raw.get('generation')
                            or proof.get('upf_epoch') != owner.get('upfEpoch')
                            or v2 and proof.get('native_upf_id') != owner.get('nativeUpfId')
                            or not proof.get('sm_context_ref') or not proof.get('smf_boot_epoch')
                            or type(proof.get('session_created_at')) not in (int, float)
                            or not 0 < proof['session_created_at'] <= self.clock()):
                        raise ValueError('Unbound native identity')
                    binding = dict(proof['binding'])
                    if binding['key']['established_epoch'] != proof['sm_context_ref']:
                        raise ValueError('Session establishment epoch differs from native context')
                except Exception:
                    raise ContractError('native_session_inventory_binding_unavailable') from None
            else:
                matches = [b for b in mapped if b.get('native_supi') == owner.get('supi')
                           and b.get('native_session_id') == owner.get('sessionId')
                           and b.get('native_generation') == raw.get('generation')]
                if len(matches) != 1:
                    raise ContractError('exact_session_binding_not_commissioned')
                binding = dict(matches[0]['binding'])
            binding['key'] = SessionKey(**binding['key'])
            binding['flows'] = tuple(FlowBinding(**f) for f in binding.get('flows', ()))
            session = SessionBinding(**binding)
            if (session.key.supi != owner.get('supi')
                    or str(session.key.pdu_session_id) != str(owner.get('sessionId'))
                    or session.upf_id != owner.get('upf')):
                raise ContractError('commissioned_binding_differs_from_native_identity')
            if v2 and session.key.token != owner['sessionKey']:
                raise ContractError('commissioned_binding_differs_from_native_identity')
            from backend.onprem.scenarios.observability import _measurement_interval
            application_reports = self._application_reports(raw, session)
            reports = application_reports if application_reports is not None else row.get('windows', ())
            intervals = [_measurement_interval(report) for report in reports]
            if not intervals or any(a is None or b is None for a, b in intervals):
                raise ContractError('receiver_window_clock_missing')
            directional = any(report.get('direction') in ('ul','dl') for report in reports)
            if directional:
                directions = [report.get('direction') for report in reports]
                if len(set(directions)) != len(directions) or any(d not in ('ul','dl') for d in directions):
                    raise ContractError('ambiguous_receiver_direction')
                for report in reports:
                    native = report.get('binding', {})
                    if (report.get('source') != 'native-iperf3-receiver'
                            or native.get('supi') != session.key.supi
                            or str(native.get('sessionId')) != str(session.key.pdu_session_id)
                            or (native.get('smContextRef') != session.key.established_epoch
                                and (report.get('serviceRoute') or {}).get('session_key') != session.key.token)):
                        raise ContractError('receiver_direction_native_binding_mismatch')
                    if v2 and any(native.get(k) != owner.get(k) for k in IDENTITY_FIELDS):
                        raise ContractError('receiver_direction_native_binding_mismatch')
            if not directional and len(set(intervals)) != 1:
                # Window averages cannot be re-integrated over their intersection.
                raise ContractError('receiver_windows_not_synchronized')
            started, finished = max(a for a, _ in intervals), min(b for _, b in intervals)
            if not directional and not started < finished:
                raise ContractError('receiver_windows_do_not_overlap')
            if reader is not None and min(a for a,_ in intervals) < proof['session_created_at']:
                raise ContractError('receiver_window_predates_native_session')
            times.extend(b for _,b in intervals)
            sessions.append(session)
            native_paths.append((session.key.token, owner.get('upfEpoch')))
            if directional:
                for report,(direction_started,direction_finished) in zip(reports,intervals):
                    direction=report['direction']
                    duration=direction_finished-direction_started
                    received=report.get('receivedMbps')
                    offered=report.get('offeredObservedMbps')
                    for name,value in ((f'{direction}_throughput',received),
                                       (f'{direction}_received_bps',received),
                                       (f'{direction}_offered_bps',offered)):
                        if value is not None:
                            metrics.append(Metric(name,finite(value,'native directional rate',0)*1_000_000,'bps','native-iperf3-receiver',
                                direction_finished,duration,session.key.token))
                if len(set(intervals)) != 1 or set(directions)!={'ul','dl'}:
                    # Directional averages are valid individually, but cannot be
                    # relabeled as combined/loss/latency on a synthetic interval.
                    continue
            for name, field, unit, factor in (
                ('ul_throughput', 'receivedUplinkMbps', 'bps', 1_000_000),
                ('dl_throughput', 'receivedDownlinkMbps', 'bps', 1_000_000),
                ('rtt', 'rttMs', 'ms', 1), ('loss', 'lossPercent', 'percent', 1),
            ):
                value = row.get(field)
                if directional and name in {'ul_throughput','dl_throughput'}:
                    continue
                # Aggregated RTT is not a per-session latency measurement.
                if value is None or name == 'rtt' and row.get('rttSource') != 'receiver-session':
                    continue
                metrics.append(Metric(name, value * factor, unit, 'native-receiver', finished,
                                      finished - started, session.key.token))
        membership = MembershipResolver().resolve(intent, sessions)
        tokens = {m.session_token for m in membership.members}
        requested = self._current_requested(raw, sessions)
        telemetry_rows = []
        for session in sessions:
            rates = requested.get(session.key.token)
            own = [m for m in metrics if m.resource_id == session.key.token and m.name in {'ul_throughput', 'dl_throughput'}]
            if rates and own:
                for direction in ('ul','dl'):
                    receiver=next((m for m in own if m.name==direction+'_throughput'),None)
                    if receiver:
                        metrics.append(Metric(direction+'_requested_bps',rates[direction],'bps',
                            'requested-traffic-configuration',receiver.observed_at,receiver.window_seconds,session.key.token))
            if len(own) != 2 or len({(m.observed_at,m.window_seconds) for m in own}) != 1:
                continue
            actual=[m for m in metrics if m.resource_id==session.key.token and m.name in {'ul_offered_bps','dl_offered_bps'}]
            if (len(actual)!=2 or len({(m.observed_at,m.window_seconds) for m in own+actual})!=1):
                continue
            at, window = own[0].observed_at,own[0].window_seconds
            demand = sum(m.value for m in actual)
            received = sum(m.value for m in own)
            telemetry_rows.append({'session_key': session.key.token,
                'source': 'native-accepted-traffic-window', 'accepted': True,
                'observed_at': at, 'window_seconds': window,
                'offered_bps': demand, 'received_bps': received})
            metrics.extend((Metric('offered_bps', demand, 'bps', 'native-iperf3-sender', at, window, session.key.token),
                            Metric('received_bps', received, 'bps', 'native-receiver', at, window, session.key.token)))
            if demand > 0:
                metrics.append(Metric('demand_satisfaction', min(100, received / demand * 100), 'percent',
                                      'native-receiver/offered', at, window, session.key.token))
        for objective in intent.objectives:
            if objective.scope == 'group' and objective.statistic == 'sum' and objective.metric in {'ul_throughput', 'dl_throughput'}:
                values = [m for m in metrics if m.name == objective.metric and m.resource_id in tokens]
                if (len(values) == len(tokens) and values
                        and len({(m.observed_at - m.window_seconds, m.observed_at) for m in values}) == 1):
                    metrics.append(Metric(objective.metric, sum(m.value for m in values), objective.unit,
                        'native-receiver-aggregate', min(m.observed_at for m in values),
                        min(m.window_seconds for m in values), intent.intent_id, statistic='sum', scope='group'))
        target_measurements = []
        for target, cohort in getattr(resolved, 'target_session_keys', ()):
            values = [m for m in metrics if m.name == 'offered_bps' and m.resource_id in cohort]
            if (cohort and len(values) == len(cohort)
                    and {m.resource_id for m in values} == set(cohort)
                    and len({(m.observed_at, m.window_seconds) for m in values}) == 1):
                resource_id = target + ':offered'
                metrics.append(Metric('offered_bps', sum(m.value for m in values), 'bps',
                    'native-complete-nf-cohort/accepted-traffic', values[0].observed_at,
                    values[0].window_seconds, resource_id, scope='resource'))
                target_measurements.append((target, (resource_id,)))
                nf = self.config.get('nf_telemetry_targets', {}).get(target)
                pod = nf.get('pod') if isinstance(nf, dict) else nf
                if pod and pod != target:
                    target_measurements.append((pod, (resource_id,)))
        capacities, target_versions = (), ()
        missing=[]
        if self.resource_telemetry is not None:
            telemetry = self.resource_telemetry.collect(tuple(sessions), tuple(telemetry_rows))
            missing.extend(telemetry.get('missing',()))
            identities = {(m.name, m.resource_id, m.statistic, m.scope): m for m in metrics}
            for metric in telemetry['metrics']:
                identity = (metric.name, metric.resource_id, metric.statistic, metric.scope)
                if identity in identities:
                    if metric.value != identities[identity].value or metric.unit != identities[identity].unit:
                        raise ContractError('native_telemetry_metric_conflict')
                else:
                    metrics.append(metric)
            capacities = tuple(sorted(telemetry['available_resources'].items()))
            target_versions = tuple(sorted(telemetry['target_versions'].items()))
        nf_configuration = []
        if self.nf_load_reader is not None:
            native_load = self.nf_load_reader.collect()
            missing.extend(native_load.get('missing',()))
            metrics.extend(native_load['metrics'])
            measurement_map = dict(target_measurements)
            for target, identities in native_load['target_measurement_ids']:
                measurement_map[target] = identities
                nf = self.config.get('nf_telemetry_targets', {}).get(target)
                pod = nf.get('pod') if isinstance(nf, dict) else nf
                if pod:
                    measurement_map[pod] = identities
            target_measurements = list(sorted(measurement_map.items()))
            for row in native_load.get('observations', ()):
                nf_configuration.append({key: row.get(key) for key in
                    ('target_id','pod_uid','resource_version','container_id','restart_count',
                     'resources','image','image_id','upf_epoch','netns','evidence_service_uid')})
        revision = hashlib.sha256(json.dumps([
            raw.get('roundId'), raw.get('generation'), raw.get('policyRevision'), raw.get('populationRevision',0),
            sorted((s.key.token, s.path_revision) for s in sessions), sorted(native_paths), target_versions,
            tuple(getattr(resolved,'target_session_keys',())),
            getattr(resolved, 'census_revision', None), getattr(resolved, 'target_session_keys', ()),
            *([{'commissioned_upfs':resolved.registry_revision}] if getattr(resolved,'registry_revision',None) else []),
            target_measurements, nf_configuration,
        ], sort_keys=True).encode()).hexdigest()
        observed = min(times)
        from .observations import missing_measurements
        requests=missing_measurements(sessions,metrics,missing)
        observation_id = hashlib.sha256(json.dumps([revision, observed, [asdict(m) for m in metrics]], sort_keys=True).encode()).hexdigest()
        planning_context = self._planning_context(raw, intent, sessions)
        return ObservationSnapshot(observation_id, observed, revision, tuple(sessions), tuple(metrics), capacities,
                                   maximum_age_seconds=self.maximum_age, target_versions=target_versions,
                                   traffic_request_id=raw.get('requestId', ''),
                                   target_measurement_ids=tuple(target_measurements),
                                   native_target_session_keys=tuple(getattr(resolved,'target_session_keys',())),
                                   measurement_requests=requests, **planning_context)

    @staticmethod
    def _current_offered(raw, sessions):
        """Actual sender reports only; requested traffic never becomes evidence."""
        rates={}
        by_identity={(s.key.supi,str(s.key.pdu_session_id)):s for s in sessions}
        for row in raw.get('measurements',()):
            for report in row.get('windows',()):
                binding=report.get('binding',{})
                session=by_identity.get((binding.get('supi'),str(binding.get('sessionId'))))
                direction=report.get('direction')
                value=report.get('offeredObservedMbps')
                if (session is None or direction not in ('ul','dl')
                        or report.get('source')!='native-iperf3-receiver'
                        or binding.get('smContextRef')!=session.key.established_epoch
                        or type(value) not in (int,float) or not math.isfinite(value) or value<0):
                    continue
                target=rates.setdefault(session.key.token,{})
                if direction in target:
                    return {}
                target[direction]=value*1_000_000
        return rates

    @staticmethod
    def _current_requested(raw, sessions):
        actions = raw.get('actions', ())
        aggregates = [a for a in actions if a.get('kind') == 'traffic_aggregate']
        if aggregates:
            latest = aggregates[-1]
            ids = latest.get('shardActionIds')
            if latest.get('accepted') is not True or not isinstance(ids, list):
                return {}
            windows = [a for a in actions if a.get('id') in ids]
            if len(windows) != len(ids) or [a.get('id') for a in windows] != ids:
                return {}
        else:
            windows = [a for a in actions if a.get('kind') == 'traffic_window'][-1:]
        if not windows or any(a.get('accepted') is not True or a.get('cleanupVerified') is not True for a in windows):
            return {}
        unique_supis = len({s.key.supi for s in sessions}) == len(sessions)
        rates = {}
        for window in windows:
            explicit = window.get('offeredBySession')
            if isinstance(explicit, dict):
                values = explicit
            elif unique_supis and isinstance(window.get('offeredBySupi'), dict):
                values = {s.key.token: window['offeredBySupi'][s.key.supi] for s in sessions
                          if s.key.supi in window['offeredBySupi']}
            else:
                return {}
            for key, value in values.items():
                if key in rates:
                    return {}
                from backend.onprem.agents.native_observation import valid_rate, direction_rate
                if not valid_rate(value):
                    return {}
                rates[key] = {direction: direction_rate(value, direction) * 1000 for direction in ('ul', 'dl')}
        return rates if set(rates) == {s.key.token for s in sessions} else {}


class ServiceEffectEvaluator:
    def __init__(self, *, protected_intents=None, maximum_regression_percent=5, clock=time.time):
        self.protected_intents = protected_intents or (lambda: ())
        self.maximum_regression = float(maximum_regression_percent)
        if not 0 <= self.maximum_regression <= 100:
            raise ContractError('invalid_protected_regression_limit')
        self.clock = clock

    def __call__(self, intent, before, after, membership, operation_id, *, changed=True, verification_not_before=None, replaced_session_keys=()):
        now = self.clock()
        tokens = tuple(sorted({m.session_token for m in membership.members}))
        flows = tuple(sorted({m.session_token + ':' + m.flow_id for m in membership.members if m.flow_id}))
        current = MembershipResolver().resolve(intent, after.sessions)
        same_members = current == membership and not membership.rejected
        verdicts = [evaluate_objective(o, after, (intent.intent_id,) if o.scope == 'group'
                    else tokens if o.scope == 'per_session' else flows, now) for o in intent.objectives]
        verdicts_met = bool(verdicts) and all(v.status == 'passed' for v in verdicts)
        # Non-target sessions are protected in both directions, including absent data.
        protected = {s.key.token for s in before.sessions} - set(tokens) - set(replaced_session_keys)
        comparisons = []
        for token in sorted(protected):
            for name in ('ul_throughput', 'dl_throughput'):
                old = [m for m in before.metrics if m.name == name and m.resource_id == token and m.validity == 'valid']
                new = [m for m in after.fresh_metrics(now) if m.name == name and m.resource_id == token and m.unit == 'bps' and m.statistic == 'mean' and m.scope == 'per_session']
                passed = len(old) == len(new) == 1 and old[0].unit == 'bps' and new[0].observed_at > old[0].observed_at
                if passed:
                    passed = new[0].value >= old[0].value * (1 - self.maximum_regression / 100)
                comparisons.append({'session_key': token, 'metric': name, 'passed': passed})
        protected_contracts = []
        for other in self.protected_intents():
            if other.intent_id == intent.intent_id or not other.is_active(now):
                continue
            selected = MembershipResolver().resolve(other, after.sessions)
            for objective in other.objectives:
                ids = (other.intent_id,) if objective.scope == 'group' else tuple(sorted({m.session_token for m in selected.members})) if objective.scope == 'per_session' else tuple(sorted({m.session_token + ':' + m.flow_id for m in selected.members if m.flow_id}))
                protected_contracts.append(asdict(evaluate_objective(objective, after, ids, now)))
        # Includes protected services' latency/loss windows, not just target throughput.
        used_metrics = after.fresh_metrics(now)
        window_started_at = min((m.observed_at - m.window_seconds for m in used_metrics), default=0)
        cutoff = before.observed_at if verification_not_before is None else verification_not_before
        fresh_window = (not changed or after.observation_id != before.observation_id
                        and after.observed_at > before.observed_at and window_started_at >= cutoff)
        same_demand = before.demand_revision == after.demand_revision
        same_cohort = before.cohort_digest == after.cohort_digest
        old_load = {m.resource_id: m.value for m in before.metrics if m.name == 'offered_bps' and m.resource_id in tokens}
        new_load = {m.resource_id: m.value for m in after.fresh_metrics(now) if m.name == 'offered_bps' and m.resource_id in tokens}
        comparable_load = (old_load.keys() == new_load.keys() and bool(old_load)
            and all(abs(new_load[k] - v) <= max(1, abs(v) * .05) for k, v in old_load.items()))
        return {'met': same_members and fresh_window and verdicts_met,
                'protected_services_met': all(x['passed'] for x in comparisons) and all(x['status'] == 'passed' for x in protected_contracts),
                'owner_id': intent.intent_id, 'operation_id': operation_id, 'session_keys': list(tokens),
                'source': 'current-native-receiver', 'observation_id': after.observation_id,
                'observed_at': after.observed_at, 'window_started_at': window_started_at,
                'same_membership': same_members,
                'comparison_eligible': same_demand and same_cohort and comparable_load,
                'comparison_basis': 'same-demand-same-cohort-offered-load-within-five-percent',
                'path_changed': before.path_bindings_ref != after.path_bindings_ref,
                'receiver_path_evidence': [p for route in after.path_evidence
                    for p in route.get('receiver_path_evidence', ())],
                'fresh_window': fresh_window, 'objectives': [asdict(v) for v in verdicts],
                'protected_sessions': comparisons, 'protected_contracts': protected_contracts}


def validate_formal_device_runtime(context, config):
    """The production intent entry uses one commissioned FreeRAN native stack."""
    controller = getattr(context, 'ue_controller', None)
    driver = getattr(controller, 'driver', None)
    backend = getattr(driver, 'backend', None)
    if driver is not None and backend != 'free-ran-ue':
        raise ContractError('formal_intent_requires_free_ran_ue_driver')
    profile_data = config.get('ue_device_profile')
    if profile_data is None:
        if config.get('requires_pre_traffic_admission') is True and backend == 'free-ran-ue':
            raise ContractError('formal_free_ran_device_profile_not_configured')
        return
    from backend.onprem.runtime.device_sessions import DeviceDriverProfile
    profile = DeviceDriverProfile(**profile_data)
    actual_profile = getattr(driver, 'device_profile', None)
    if (profile.driver != 'free-ran-ue' or backend != 'free-ran-ue'
            or actual_profile != profile or not config.get('ue_device_image')
            or getattr(driver, 'image', None) != config['ue_device_image']
            or config['ue_device_image'].split('@')[-1] != profile.image_digest):
        raise ContractError('formal_free_ran_device_runtime_configuration_mismatch')


def configured_intent_service(context):
    config = load_configuration()
    validate_formal_device_runtime(context, config)
    from .current_upf_scope import configured_current_upfs
    current_upfs=getattr(context,'intent_current_upfs',None) or configured_current_upfs(config)
    authority_config = config.get('authority', {})
    authority = PolicyAuthority(**{name: frozenset(authority_config.get(name, ()))
        for name in PolicyAuthority.__dataclass_fields__})
    from .actuators import build_registry
    from .resource_adapters import configured_resource_providers
    providers = configured_resource_providers(context, config.get('providers', {}))
    registry = build_registry(providers)
    if config.get('native_nf_load') and getattr(context, 'intent_nf_load_reader', None) is None:
        from .native_nf_load import NativeNfLoadReader
        from .resource_adapters import KubectlResourceClient
        values = dict(config['native_nf_load'])
        kubernetes = KubectlResourceClient(context=values.pop('context'), namespace=values.pop('namespace'))
        context.intent_nf_load_reader = NativeNfLoadReader(kubernetes, **values)
    observer = CurrentCityObserver(context, config)
    service_catalog = config.get('service_catalog', config.get('ue_classification', {}).get('service_mapping', {}))
    if service_catalog:
        from backend.onprem.traffic.session_workload import SessionWorkloadCompiler
        workload_settings = config.get('session_workload', {})
        if not isinstance(workload_settings, dict):
            raise ContractError('invalid_server_session_workload_configuration')
        compiler = SessionWorkloadCompiler(service_catalog,
            functional_combined_budget_kbps=workload_settings.get('functional_combined_budget_kbps'))
        context.intent_session_workload_compiler = compiler
        resources = getattr(getattr(context, 'round_coordinator', None), 'resources', None)
        if resources is not None:
            resources.session_workload_compiler = compiler
    if config.get('session_inventory') and observer.session_binding_reader is None:
        from .native_session_inventory import NativeSessionBindingReader
        classification_reader = getattr(context, 'intent_session_classification_reader', None)
        if classification_reader is None and config.get('ue_classification'):
            from .native_ue_classification import NativeUeClassificationReader
            from .resource_adapters import KubectlResourceClient
            from backend.onprem.runtime.free_ran_ue import CONFIG_INIT
            values = dict(config['ue_classification'])
            if values.pop('configuration_sha256', None) != hashlib.sha256(CONFIG_INIT.encode()).hexdigest():
                raise ContractError('ue_classification_initializer_digest_changed')
            kubernetes = KubectlResourceClient(context=values.pop('context'), namespace=values.pop('namespace'))
            classification_reader = NativeUeClassificationReader(kubernetes,
                generation_reader=context.ue_controller.store.current, configuration_source=CONFIG_INIT, **values)
        from .native_device_classification import NativeDeviceClassificationReader
        controller = getattr(context, 'ue_controller', None)
        generation_reader = getattr(getattr(controller, 'store', None), 'current', None)
        if callable(generation_reader) and callable(getattr(controller, 'status', None)):
            classification_reader = NativeDeviceClassificationReader(status_reader=controller.status,
                generation_reader=generation_reader, legacy_reader=classification_reader,
                catalogue=config['session_inventory'].get('catalogue', ()))
        qos_provider = providers.get('qos')
        if qos_provider is None:
            raise ContractError('native_session_inventory_requires_configured_smf_client')
        observer.session_binding_reader = NativeSessionBindingReader(qos_provider.api,
            **config['session_inventory'],
            classification_reader=classification_reader,current_upfs=current_upfs)
    from .resource_telemetry import NativeResourceTelemetry
    path_capacity_reader = getattr(context, 'intent_path_capacity_reader', None)
    if path_capacity_reader is None and (config.get('path_capacity_calibrations') or config.get('path_capacity_publication') is True):
        from .native_path_capacity import NativeCalibratedPathCapacityReader,NativePathHardwareReader
        if providers.get('qos') is None or getattr(context, 'intent_nf_load_reader', None) is None:
            raise ContractError('path_calibration_requires_current_native_census_and_nf_load')
        def current_sender_load(sessions):
            ledger=context.round_ledger
            opening=ledger.batch_control()
            if not opening.get('roundId') or opening.get('stage') in {'completed','cleaning'}:
                raise ContractError('path_calibration_current_sender_missing')
            raw=ledger.live_snapshot(opening['roundId'])
            rates=CurrentCityObserver._current_offered(raw,sessions)
            from backend.onprem.scenarios.observability import _measurement_interval
            intervals=[_measurement_interval(r) for row in raw.get('measurements',()) for r in row.get('windows',())]
            closing=ledger.batch_control()
            if (opening.get('roundId')!=closing.get('roundId') or opening.get('generation')!=closing.get('generation')
                    or closing.get('stage') in {'completed','cleaning'} or not intervals
                    or len(rates)!=len(sessions) or any(set(v)!= {'ul','dl'} for v in rates.values())
                    or any(a is None or b is None for a,b in intervals)):
                raise ContractError('path_calibration_current_sender_changed')
            return {'source':'native-current-sender-load/v1','observed_at':min(b for _,b in intervals),'rates_bps':rates}
        path_capacity_reader = NativeCalibratedPathCapacityReader(providers['qos'].api,
            context.intent_nf_load_reader, config.get('path_capacity_calibrations',()),
            hardware_reader=NativePathHardwareReader(context.intent_nf_load_reader.client,
                context.intent_nf_load_reader.targets),offered_reader=current_sender_load,
            publication_store=Path(context.settings.state_file).with_name('intents.sqlite3')
                if config.get('path_capacity_publication') is True else None)
        context.intent_path_capacity_reader=path_capacity_reader
    observer.resource_telemetry = getattr(context, 'intent_resource_telemetry', None) or NativeResourceTelemetry(
        providers, qos_targets=config.get('qos_telemetry_targets'), nf_targets=config.get('nf_telemetry_targets'),
        path_capacity_reader=path_capacity_reader,
        resource_metric_reader=getattr(context, 'intent_resource_metric_reader', None))
    planner = getattr(context, 'intent_planner', None)
    if planner is None and config.get('budgets'):
        from .model_planner import ResourceBudget, TargetPolicy, build_resource_planner
        from backend.onprem.integrations.openai.openai_client import OpenAIClient, OpenAIError
        try:
            client = OpenAIClient.from_env()
        except OpenAIError:
            client = None
        if client is not None:
            planner = build_resource_planner(client=client, authority=authority, registry=registry,
                budgets={key: ResourceBudget(**value) for key, value in config['budgets'].items()},
                targets=tuple(TargetPolicy(**value) for value in config.get('targets', ())),
                profiles=tuple(CapacityProfile(**value) for value in config.get('capacity_profiles', ())))
    database = Path(context.settings.state_file).with_name('intents.sqlite3')
    repository = IntentRepository(database)
    def admission():
        if context.application_shutting_down or context.maintenance_blocked():
            raise ContractError('city_runtime_not_available')
        if not context.runtime_policy_clear():
            raise ContractError('native_policy_ownership_unresolved')
    def protected_intents():
        # Only live contracts are read; evidence/report archives are never consulted.
        return (ServiceIntent.from_dict({k: v for k, v in record.items() if k in ServiceIntent.__dataclass_fields__})
                for _owner, record in repository.iter_active() if 'text' not in record)
    if config.get('native_nf_services'):
        from .native_nf_services import NativeNfServiceInventoryReader
        from .native_kubernetes_observer import NativeKubernetesObserver
        from .resource_adapters import KubectlResourceClient
        values=dict(config['native_nf_services'])
        kubernetes=KubectlResourceClient(context=values.pop('context'),namespace=values.pop('namespace'))
        context.intent_nf_service_inventory_reader=NativeNfServiceInventoryReader(kubernetes,
            observer.session_binding_reader,raw_reader=context.round_ledger.current_service_scope,
            active_intents=protected_intents,**values)
        provider=providers.get('kubernetes')
        if provider is None or config.get('providers',{}).get('kubernetes',{}).get('observer_origin'):
            raise ContractError('native_nf_services_requires_local_kubernetes_provider')
        provider.service_reader=NativeKubernetesObserver(kubernetes,pod_allowlist=provider.pods,
            service_inventory_reader=context.intent_nf_service_inventory_reader).services
    evaluator = ServiceEffectEvaluator(protected_intents=protected_intents,
        maximum_regression_percent=config.get('maximum_protected_regression_percent', 5))
    async def start_traffic(body, *, intent, operation_id, owner, reservation=None):
        from backend.onprem.scenarios.contracts import validate_scenario_request
        from backend.onprem.traffic.admission import admit_traffic
        from backend.onprem.application.scenarios import ScenarioService
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
        from .service import invoke
        validate_scenario_request(body, allow_none=True)
        admit_traffic(body)
        current = await invoke(context.round_ledger.intent_traffic_lease)
        lease = ServiceIntentTrafficLease.issue(intent, owner, operation_id, body['requestId'])
        if current:
            existing = ServiceIntentTrafficLease.from_dict(current['lease'])
            if (existing.intent_id, existing.owner_id, existing.operation_id, existing.request_id) == (
                    intent.intent_id, owner, operation_id, body['requestId']):
                lease = existing
        result = await ScenarioService(context).start_intent_traffic(body, lease,
            **({'held_reservation': reservation} if reservation is not None else {}))
        return {'requestId': body['requestId'], 'accepted': True, 'source': 'scenario-service',
                'service_intent_traffic_lease': lease.to_dict(), 'traffic': result}

    async def traffic_start_readback(intent, owner, operation_id, request_id):
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease
        from .service import invoke
        current = await invoke(context.round_ledger.intent_traffic_lease)
        if not current:
            return None
        lease = ServiceIntentTrafficLease.from_dict(current['lease'])
        if (lease.intent_id, lease.owner_id, lease.operation_id, lease.request_id) != (
                intent.intent_id, owner, operation_id, request_id):
            raise ContractError('native_traffic_start_readback_owner_changed')
        return {'requestId': request_id, 'accepted': True, 'source': 'scenario-service-readback',
                'service_intent_traffic_lease': lease.to_dict()}

    async def release_traffic(intent, owner, release_operation_id):
        from backend.onprem.application.scenarios import ScenarioService
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease, IntentTrafficRestoration
        from .service import invoke
        current = await invoke(repository.current_intent_traffic, intent.intent_id, owner)
        if not current or current.get('native_restoration_complete') is not True:
            raise ContractError('native_restoration_required_before_traffic_release')
        lease = ServiceIntentTrafficLease.from_dict(current['receipt']['service_intent_traffic_lease'])
        if (lease.intent_id != intent.intent_id or lease.owner_id != owner
                or lease.operation_id != current['start_operation_id']):
            raise ContractError('native_traffic_release_owner_changed')
        # Renewal spans two durable journals. Cleanup uses the actual round lease
        # after checking immutable ownership, including a crash between the CASes.
        round_entry = await invoke(context.round_ledger.intent_traffic_lease)
        if not round_entry:
            raise ContractError('native_traffic_release_round_missing')
        round_lease = ServiceIntentTrafficLease.from_dict(round_entry['lease'])
        if any(getattr(round_lease, key) != getattr(lease, key) for key in
               ('lease_id', 'intent_id', 'owner_id', 'operation_id', 'request_id', 'valid_from')):
            raise ContractError('native_traffic_release_round_owner_changed')
        lease = round_lease
        proof = IntentTrafficRestoration.for_lease(lease, release_operation_id)
        result = await ScenarioService(context).finish_intent_traffic(lease, proof)
        return {'released': isinstance(result, dict) and result.get('released') is True,
                'lease_id': lease.lease_id, 'requestId': lease.request_id,
                'source': 'intent-owned-traffic-release', 'traffic': result}

    async def drain_traffic(intent, owner, release_operation_id):
        from backend.onprem.application.scenarios import ScenarioService
        from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease, IntentTrafficDrainPermit
        from .service import invoke
        operation = await invoke(repository.operation, release_operation_id, owner)
        if (operation['intent_id'] != intent.intent_id
                or (operation['kind'] != 'release' and operation.get('failure_cleanup') is not True)
                or operation.get('policy_restoration_complete') is not True
                or operation.get('admissions_closed') is not True):
            raise ContractError('native_policy_restoration_required_before_traffic_drain')
        current = await invoke(repository.current_intent_traffic, intent.intent_id, owner)
        if not current:
            raise ContractError('native_traffic_drain_ownership_missing')
        lease = ServiceIntentTrafficLease.from_dict(current['receipt']['service_intent_traffic_lease'])
        round_entry = await invoke(context.round_ledger.intent_traffic_lease)
        if not round_entry:
            raise ContractError('native_traffic_drain_round_missing')
        round_lease = ServiceIntentTrafficLease.from_dict(round_entry['lease'])
        if (lease.operation_id != current['start_operation_id'] or lease.owner_id != owner
                or lease.intent_id != intent.intent_id or any(getattr(round_lease, key) != getattr(lease, key)
                    for key in ('lease_id','intent_id','owner_id','operation_id','request_id','valid_from'))):
            raise ContractError('native_traffic_drain_owner_changed')
        permit = IntentTrafficDrainPermit.for_lease(round_lease, release_operation_id)
        result = await ScenarioService(context).drain_intent_traffic(round_lease, permit)
        return {'drained': isinstance(result, dict) and result.get('drained') is True,
                'native_ues_gone': isinstance(result, dict) and result.get('native_ues_gone') is True,
                'lease_id': round_lease.lease_id, 'source': 'intent-owned-traffic-drain', 'traffic': result}

    async def request_expired_traffic_release(lease):
        # Enqueue only: waiting here would cycle with the coordinator's cleanup task.
        return await context.intent_service.reconcile(lease.intent_id, lease.owner_id,
            'traffic-expiry:' + lease.lease_id + ':' + str(lease.intent_revision),
            release=True, expected_revision=lease.intent_revision)

    async def renew_traffic(lease):
        from .service import invoke
        return await invoke(repository.renew_intent_traffic, lease, time.time())

    context.intent_traffic_restorer = request_expired_traffic_release
    context.intent_traffic_renewer = renew_traffic
    pretraffic=getattr(context,'intent_pre_traffic_admission',None)
    admission_coordinator=getattr(context,'intent_admission_coordinator',None)
    from .baseline_device_admission import configured_baseline_device_admission
    baseline_pretraffic=configured_baseline_device_admission(context,config,authority)
    if baseline_pretraffic is not None:
        pretraffic=baseline_pretraffic
    if config.get('requires_pre_traffic_admission') is True:
        if admission_coordinator is None:
            qos_api=getattr(providers.get('qos'),'api',None)
            if qos_api is not None and all(hasattr(qos_api,k) for k in ('origin','token_reader','timeout')):
                from .pending_admission import PendingAdmissionCoordinator,FixedOriginSmfAdmissionApi
                admission_coordinator=PendingAdmissionCoordinator(FixedOriginSmfAdmissionApi(qos_api.origin,
                    token_reader=qos_api.token_reader,timeout=qos_api.timeout))
        if pretraffic is None and admission_coordinator is not None and all(hasattr(planner,k) for k in ('provider','validator')):
            from functools import partial
            from .pretraffic_planning import PreTrafficAdmissionPlanner,PreTrafficAdmissionPreparer,requested_city_workload,NativePreTrafficObservationSource,ReservedServiceCatalog
            from backend.onprem.runtime.ue_reservation import UEAdmissionLaunchProfile
            profile_reader=getattr(context,'intent_ue_launch_profile_reader',None)
            if profile_reader is None and config.get('ue_launch_profile'):
                pinned_profile=UEAdmissionLaunchProfile.from_dict(config['ue_launch_profile'])
                profile_reader=lambda _body:pinned_profile
            bootstrap=None
            if 'bootstrap_infrastructure' in config:
                from .pretraffic_infrastructure_ports import configured_bootstrap_bridge
                bootstrap=configured_bootstrap_bridge(context,config['bootstrap_infrastructure'],
                    repository,providers,admission_coordinator)
            pretraffic=PreTrafficAdmissionPreparer(context.ue_controller,
                PreTrafficAdmissionPlanner(planner.provider,planner.validator,
                    bootstrap=bootstrap,
                    observation_source=getattr(context,'intent_pretraffic_observation_source',None) or
                        NativePreTrafficObservationSource(observer.resource_telemetry,getattr(context,'intent_nf_load_reader',None)),
                    demand_reader=getattr(context,'intent_pretraffic_requested_workload_reader',None) or
                        partial(requested_city_workload,session_compiler=getattr(context,'intent_session_workload_compiler',None))),
                admission_coordinator,profile_reader=profile_reader,
                classification_reader=getattr(context,'intent_reserved_classification_reader',None) or
                    ReservedServiceCatalog({scenario:dict(record,configuration_id=record.get('configuration_id') or
                        'service-catalog:'+hashlib.sha256(json.dumps(service_catalog,sort_keys=True).encode()).hexdigest())
                        for scenario,record in service_catalog.items()}),
                network_scope=config.get('session_inventory',{}).get('network_scope',''))
    service = IntentService(repository, authority=authority, observer=observer, planner=planner,
        registry=registry, evaluator=evaluator, admission=admission, traffic_starter=start_traffic,
        pre_traffic_admission=pretraffic,
        requires_pre_traffic_admission=config.get('requires_pre_traffic_admission') is True,
        admission_coordinator=admission_coordinator,
        traffic_releaser=release_traffic, traffic_start_readback=traffic_start_readback,
        traffic_drainer=drain_traffic,
        population_coordinator=getattr(context,'intent_population_coordinator',None))
    if service.population_coordinator is None and config.get('population'):
        from .population_bridge import build_native_population_coordinator
        service.population_coordinator=build_native_population_coordinator(context,config,service)
    empty_reader=getattr(service.population_coordinator,'current_empty_proof',None)
    if callable(empty_reader):
        observer.empty_population_reader=empty_reader
    return service
