import { decodeScopedTrafficCapabilities, type ScopedTrafficCapability } from './scopedTrafficCapabilities'
import { apiBaseUrl } from '../../services/runtimeConfig';
import { validServiceRequirements, validServiceSla, validServiceWorkload, type ServiceRequirements, type ServiceSlaAssessment } from './serviceRequirements';
import { sessionHeaders } from '../../services/browserSession';
import { type SpatialDemand } from './spatialDemand';
import { SERVICE_PROFILES, SCENARIO_SLICES, SCENARIO_EVENTS, MAX_SCENARIO_UES, type ScenarioEvent, type ScenarioGroup } from '../../domain/scenarios';
import { nativeReceiverDirections, validatedNativeBindings, type NativeSessionBindings } from './nativeSessionBindings';
export { SCENARIO_EVENTS, MAX_SCENARIO_UES, type ScenarioEvent, type ScenarioGroup } from '../../domain/scenarios';
const MAX_HISTORICAL_UES = 500;
export type ScenarioStrategy = 'none' | 'static' | 'ai';
export type TrafficMode = 'functional' | 'qos';
// Fixed diagnostic fields from the full/summary receipt; never render raw errors or tokens.
const DIAGNOSTIC_CODES = new Set([
    'ue.registration_rejected', 'runtime.guard_rejected', 'runtime.unexpected_failure', 'policy.blocked',
    ...['native_upf_epoch_invalid', 'aggregate_budget_invalid', 'per_ue_budget_invalid', 'binding_malformed',
        'native_identity_mismatch', 'native_binding_unavailable', 'intent_not_reserved', 'receiver_readiness_failed',
        'receiver_readiness_timeout', 'worker_output_invalid', 'worker_record_invalid', 'worker_result_incomplete',
        'native_binding_changed', 'cleanup_unverified', 'windows_not_jointly_fresh', 'window_stale_or_incomplete',
        'receiver_socket_identity_mismatch', 'client_socket_identity_mismatch', 'iperf_cookie_mismatch', 'worker_failed',
        'worker_result_invalid', 'worker_timestamp_invalid', 'receiver_timestamp_invalid', 'window_unverified',
        'offered_load_mismatch', 'failure_persistence_failed'].map((code) => `batch.${code}`),
]);
const DIAGNOSTIC_STAGES = new Set(['preflight', 'allocating', 'registering', 'establishing_pdu',
    'verifying_connectivity', 'measuring_baseline', 'measuring_result', 'results_ready', 'unknown',
    'native_preflight', 'receiver_prepare', 'client_measurement', 'native_postflight', 'evidence_reduce', 'cleanup', 'final_freshness']);
export type ScenarioAction = {
    id: string;
    kind?: string;
    workload?: RoundWorkload | SessionWorkloadSummary;
    requested: boolean;
    accepted: boolean;
    installed: boolean;
    effectVerified: boolean;
    code?: string;
    stage?: string;
    phase?: string;
    observedAt?: string;
    requestedAt?: string;
};
export function diagnosticFields(action: ScenarioAction) {
    const stage = action.kind === 'traffic_failure' ? action.phase : action.stage;
    const at = action.observedAt ?? action.requestedAt;
    return { code: typeof action.code === 'string' && DIAGNOSTIC_CODES.has(action.code) ? action.code : 'unknown',
        stage: typeof stage === 'string' && DIAGNOSTIC_STAGES.has(stage) ? stage : 'unknown',
        observedAt: typeof at === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(at) && Number.isFinite(Date.parse(at)) ? at : null };
}
export type EventCounts = Record<ScenarioEvent, number>;
export type ScenarioAllocation = {
    baselineUes: number;
    events: EventCounts;
    total: number;
};
export type ScenarioRequest = Omit<ScenarioAllocation, 'total'> & {
    requestId: string;
    strategy: ScenarioStrategy;
    trafficMode?: TrafficMode;
    serviceRequirements?: ServiceRequirements;
    topologyRevision?: number;
    demandGroups?: SpatialDemand[];
};
export type UeBinding = {
    scenario: ScenarioGroup;
    supi: string;
    sessionId?: string | number;
    podUid: string;
    ueIp?: string;
    tun?: string;
    upf?: string;
    slice?: string;
    generation?: number;
};
export type ReceiverMeasurement = UeBinding & {
    roundId: string;
    generation: number;
    policyRevision: number;
    source: string;
    observedAt: string | null;
    receivedUplinkMbps: number | null;
    receivedDownlinkMbps: number | null;
    rttMs: number | null;
};
export type CommissioningCapabilities = {
    scopedVerifiedTrafficProfiles?: ScopedTrafficCapability[];
    applicationMaxUes?: number;
    verifiedMaxUes?: number;
    verifiedMaxUesScope?: 'scenario-offered-load-v1';
    candidateMaxUes?: number | null;
    admissionMaxUes?: number;
    commissioningEnabled?: boolean;
    commissioningProfile?: string | null;
    capacityStatus?: string;
};
export type CapacityPerformance = {
    status: 'passed' | 'failed' | 'unavailable';
    criteriaId: 'capacity-delivery-v1';
    scope: 'historical-bounded-offered-load-delivery';
    reason: string;
    flowCount: number;
    failedFlowCount: number;
    failedFlows: Array<{
        stage: string;
        supi: string;
        direction: 'ul' | 'dl';
        appliedKbps: number;
        offeredAppliedRatio: number;
        receivedAppliedRatio: number;
        conservativeUniqueAppliedRatio: number;
        receiverLossPercent: number;
    }>;
    provenance: {
        roundId: string | null;
        generation: number | null;
        sequence: number;
        observedAt: string | null;
        source: 'raw-native-iperf3-reports';
        historical: true;
        currentNetworkStateClaimed: false;
    };
    promotionEligible: false;
    qosEffectClaimed: false;
};
export type RoundWorkload = {
    schemaVersion: string;
    profileId: string;
    budgetKbpsPerDirection: number;
    serviceContractRevision?: string;
    workloadKind?: 'diagnostic_probe';
    probeKbpsPerDirection?: number;
    trafficMode?: 'qos';
    perUe: Array<{
        supi: string;
        scenario: ScenarioGroup;
        slice: string;
        requestedKbps?: number;
        appliedKbps?: number;
        requestedUplinkKbps?: number;
        requestedDownlinkKbps?: number;
        appliedUplinkKbps?: number;
        appliedDownlinkKbps?: number;
    }>;
    requestedUplinkKbps: number;
    requestedDownlinkKbps: number;
    requestedCombinedKbps: number;
    appliedUplinkKbps: number;
    appliedDownlinkKbps: number;
    appliedCombinedKbps: number;
    capReason: string | null;
    comparisonFingerprint: string;
    qosEffectClaimed: false;
};
/** V2 summary intentionally contains no fabricated per-UE/session allocation. */
export type SessionWorkloadSummary = Omit<RoundWorkload, 'perUe' | 'schemaVersion' | 'profileId'> & {
    schemaVersion: 'cityverse.session-workload/v2';
    profileId: 'session-service-offered-load-v2';
    perUe?: never;
};
export function validSessionWorkloadSummary(value: unknown): value is SessionWorkloadSummary {
    const w = value as SessionWorkloadSummary;
    const fields = ['schemaVersion', 'profileId', 'budgetKbpsPerDirection', 'requestedUplinkKbps',
        'requestedDownlinkKbps', 'requestedCombinedKbps', 'appliedUplinkKbps', 'appliedDownlinkKbps',
        'appliedCombinedKbps', 'capReason', 'comparisonFingerprint', 'qosEffectClaimed', 'trafficMode'];
    return Boolean(w && Object.keys(w).sort().join(',') === fields.sort().join(',')
        && w.schemaVersion === 'cityverse.session-workload/v2' && w.profileId === 'session-service-offered-load-v2'
        && w.trafficMode === 'qos' && w.qosEffectClaimed === false && w.budgetKbpsPerDirection === 400000
        && [w.requestedUplinkKbps, w.requestedDownlinkKbps, w.appliedUplinkKbps, w.appliedDownlinkKbps]
            .every(n => Number.isSafeInteger(n) && n >= 0 && n <= w.budgetKbpsPerDirection)
        && w.requestedCombinedKbps === w.requestedUplinkKbps + w.requestedDownlinkKbps
        && w.appliedCombinedKbps === w.appliedUplinkKbps + w.appliedDownlinkKbps
        && w.appliedUplinkKbps <= w.requestedUplinkKbps && w.appliedDownlinkKbps <= w.requestedDownlinkKbps
        && /^[a-f0-9]{64}$/.test(w.comparisonFingerprint) && (w.capReason === null || typeof w.capReason === 'string'));
}
export type LegacySlaAssessment = {
    status: 'met' | 'not_met' | 'unknown';
    reason: string;
    scope: 'last-result-window';
    historical: true;
    windowId: string | null;
    scenarios: Array<{
        scenario: ScenarioGroup;
        sampleId: string | null;
        status: 'met' | 'not_met' | 'unknown';
        reason: string;
    }>;
};
export type SlaAssessment = LegacySlaAssessment | ServiceSlaAssessment;
export type ScenarioSnapshot = {
    nativeSessionBindings?: NativeSessionBindings;
    populationRevision?: number;
    lifecycle?: ScenarioLifecycle;
    serviceRequirements?: ServiceRequirements;
    resolvedRequirements?: {
        revision: string;
        source: string;
        services: Record<string, unknown>;
        slices: Record<string, unknown>;
    };
    trafficMode?: TrafficMode;
    sla?: SlaAssessment;
    agentRuntime?: {
        roundId: string;
        generation: number;
        phase: string;
        status: string;
        updatedAt: number;
        role?: string;
        tool?: string;
        model?: string;
        response_id?: string;
        cycle?: number;
        code?: string;
    };
    viewSchema?: 'cityverse.round-summary/v1';
    roundId: string | null;
    requestId?: string | null;
    generation: number | null;
    policyRevision: number;
    sequence: number;
    observedAt: string | null;
    stage: string;
    executionOutcome: 'passed' | 'failed' | 'cancelled' | null;
    cleanupStatus: string;
    allocation: ScenarioAllocation;
    requestedSliceUes?: Record<'embb' | 'urllc' | 'v2x' | 'mmtc', number>;
    trafficCounts?: {
        scope: 'selected-round-ues';
        requested: number;
        selected: number;
        registered: number | null;
        pduEstablished: number | null;
        connectivityVerified: number | null;
        observedTransmitting: number | null;
        measurementScope: 'last-receiver-window' | 'unavailable';
        observedAt: string | null;
    };
    counts: {
        allocated: number | null;
        registered: number | null;
        pduEstablished: number | null;
        trafficVerified: number | null;
    };
    capabilities: CommissioningCapabilities & {
        start?: boolean;
        staticPolicy?: boolean;
        aiPolicy?: boolean;
        reason?: string;
    };
    actions: ScenarioAction[];
    performance?: CapacityPerformance;
    perUe: UeBinding[];
    measurements: ReceiverMeasurement[];
    errors: string[];
};
export type ScenarioLifecycle = {
    schemaVersion: 'cityverse.scenario-lifecycle/v1';
    scenarioState: string;
    revision: number;
    evaluationState: string;
    resourceState: string;
    maximumCycles: 5;
    cyclesAttempted: number;
    cyclesCompleted: number;
    stopReason: string | null;
    canSubmitEvaluation: boolean;
    submissionMode: 'start' | 'queue' | 'blocked';
};
export function validSlaAssessment(value: unknown): value is SlaAssessment {
    if (value && typeof value === 'object' && 'schemaVersion' in value)
        return validServiceSla(value);
    const a = value as LegacySlaAssessment;
    const status = (v: unknown) => v === 'met' || v === 'not_met' || v === 'unknown';
    if (!a || !status(a.status) || a.scope !== 'last-result-window' || a.historical !== true
        || typeof a.reason !== 'string' || a.reason.length > 128
        || !(a.windowId === null || typeof a.windowId === 'string') || !Array.isArray(a.scenarios)
        || a.scenarios.length > 6 || new Set(a.scenarios.map((r) => r?.scenario)).size !== a.scenarios.length)
        return false;
    if (!a.scenarios.every((r) => r && ['web_browsing', ...SCENARIO_EVENTS].includes(r.scenario) && status(r.status)
        && typeof r.reason === 'string' && r.reason.length <= 128
        && (r.sampleId === null || r.sampleId === `${a.windowId}:${r.scenario}`)))
        return false;
    const states = a.scenarios.map((r) => r.status);
    return a.status === 'met' ? a.windowId !== null && states.length > 0 && states.every((s) => s === 'met')
        : a.status === 'not_met' ? a.windowId !== null && states.includes('not_met')
            : !states.includes('not_met') && (!states.length || states.includes('unknown'));
}
export function validCapacityPerformance(s: ScenarioSnapshot): boolean {
    const p = s.performance;
    if (!p || !['passed', 'failed', 'unavailable'].includes(p.status)
        || p.criteriaId !== 'capacity-delivery-v1' || p.scope !== 'historical-bounded-offered-load-delivery'
        || !['round_not_completed', 'measurements_not_ready', 'strategy_not_supported', 'evidence_invalid_or_incomplete', 'delivery_criteria_met', 'delivery_criteria_not_met'].includes(p.reason)
        || p.promotionEligible !== false || p.qosEffectClaimed !== false
        || ![p.flowCount, p.failedFlowCount].every((v) => Number.isSafeInteger(v) && v >= 0 && v <= 400)
        || !Array.isArray(p.failedFlows) || p.failedFlows.length !== p.failedFlowCount || p.failedFlowCount > p.flowCount
        || !p.provenance || p.provenance.source !== 'raw-native-iperf3-reports' || p.provenance.historical !== true
        || p.provenance.currentNetworkStateClaimed !== false
        || p.provenance.roundId !== s.roundId || p.provenance.generation !== s.generation
        || p.provenance.sequence !== s.sequence || p.provenance.observedAt !== s.observedAt)
        return false;
    if (p.status !== 'unavailable' && (!['results_ready', 'cleaning', 'completed'].includes(s.stage) || p.flowCount !== s.allocation.total * 4 || !p.flowCount))
        return false;
    if (p.status === 'passed' && (p.failedFlowCount !== 0 || p.reason !== 'delivery_criteria_met'))
        return false;
    if (p.status === 'failed' && (!p.failedFlowCount || p.reason !== 'delivery_criteria_not_met'))
        return false;
    if (p.status === 'unavailable' && (p.flowCount !== 0 || p.failedFlowCount !== 0
        || !['round_not_completed', 'measurements_not_ready', 'strategy_not_supported', 'evidence_invalid_or_incomplete'].includes(p.reason)))
        return false;
    const identities = new Set(p.failedFlows.map((r) => `${r?.stage}:${r?.supi}:${r?.direction}`));
    if (identities.size !== p.failedFlows.length)
        return false;
    return p.failedFlows.every((r) => r && ['measuring_baseline', 'measuring_result'].includes(r.stage)
        && /^imsi-[0-9]{15}$/.test(r.supi) && ['ul', 'dl'].includes(r.direction)
        && s.perUe.some((owner) => owner.supi === r.supi)
        && Number.isSafeInteger(r.appliedKbps) && r.appliedKbps > 0
        && [r.offeredAppliedRatio, r.receivedAppliedRatio, r.conservativeUniqueAppliedRatio, r.receiverLossPercent]
            .every((v) => typeof v === 'number' && Number.isFinite(v) && v >= 0)
        && r.receiverLossPercent <= 100);
}
export function admissionLimit(c: ScenarioSnapshot['capabilities'] | undefined): number | null {
    if (!c || (c.applicationMaxUes !== undefined && c.applicationMaxUes !== MAX_SCENARIO_UES)
        || (c.verifiedMaxUesScope !== undefined && c.verifiedMaxUesScope !== 'scenario-offered-load-v1')
        || typeof c.commissioningEnabled !== 'boolean' || c.staticPolicy !== false
        || !Number.isSafeInteger(c.admissionMaxUes) || !Number.isSafeInteger(c.verifiedMaxUes)
        || c.admissionMaxUes! < 1 || c.admissionMaxUes! > MAX_HISTORICAL_UES
        || c.verifiedMaxUes! < 0 || c.verifiedMaxUes! > MAX_HISTORICAL_UES) return null;
    const limit = c.admissionMaxUes!;
    // Traffic admission describes population capacity. Formal AI authority is
    // independently checked through /api/capabilities, not legacy aiPolicy.
    if (c.commissioningEnabled) {
        return c.verifiedMaxUes === 0 && c.candidateMaxUes === limit && c.aiPolicy === false
            && [5, 10, 25, 50, 100, 250, 500].includes(limit)
            && c.commissioningProfile === 'scenario-offered-load-v1'
            && (c.capacityStatus == null || c.capacityStatus === 'unverified_commissioning') ? limit : null;
    }
    if (c.commissioningProfile !== null) return null;
    if (c.candidateMaxUes === null) {
        return c.verifiedMaxUes! >= limit
            && (c.capacityStatus == null || ['receipt_verified', 'within_verified_limit'].includes(c.capacityStatus)) ? limit : null;
    }
    return c.verifiedMaxUes === 0 && c.candidateMaxUes === limit
        && c.capacityStatus === 'unverified_ai_trial' ? limit : null;
  }
export function allocationAdmitted(c: ScenarioSnapshot['capabilities'] | undefined, allocation: ScenarioAllocation | null): boolean {
    const limit = admissionLimit(c);
    if (!allocation || limit === null)
        return false;
    const parsed = parseScenarioAllocation(String(allocation.baselineUes), Object.fromEntries(SCENARIO_EVENTS.map((key) => [key, String(allocation.events[key])])) as Record<ScenarioEvent, string>);
    return Boolean(parsed && parsed.total === allocation.total && parsed.total >= 1 && parsed.total <= Math.min(limit, MAX_SCENARIO_UES));
}
export function selectedActiveScenario(current: ScenarioGroup, allocation?: ScenarioAllocation): ScenarioGroup {
    if (!allocation)
        return current;
    const count = (group: ScenarioGroup) => group === 'web_browsing' ? allocation.baselineUes : allocation.events[group];
    return count(current) > 0 ? current : (['web_browsing', ...SCENARIO_EVENTS] as ScenarioGroup[]).find((group) => count(group) > 0) ?? current;
}
export function validWorkload(value: unknown): value is RoundWorkload {
    const w = value as RoundWorkload & {
        perUe: Array<{
            supi: string;
            scenario: ScenarioGroup;
            slice: string;
            requestedKbps: number;
            appliedKbps: number;
        }>;
    };
    const integer = (n: unknown): n is number => typeof n === 'number' && Number.isSafeInteger(n) && n >= 0;
    const profiles = SERVICE_PROFILES.reduce((result, profile) => {
        result[profile.id] = [profile.slice,
            Math.round(Math.max(profile.offeredUplinkMbpsPerUe, profile.offeredDownlinkMbpsPerUe) * 1000)];
        return result;
    }, {} as Record<ScenarioGroup, readonly [string, number]>);
    if (!w || typeof w !== 'object')
        return false;
    if (w.profileId === 'scenario-service-offered-load-v1')
        return validServiceWorkload(w, profiles);
    const probe = w.profileId === 'controlled_qer_probe_v1';
    const qos = w.profileId === 'scenario-qos-offered-load-v1';
    const budget = qos ? 400000 : 10000;
    if (qos && (w.trafficMode !== 'qos' || !Array.isArray(w.perUe) || w.perUe.length < 1 || w.perUe.length > MAX_SCENARIO_UES))
        return false;
    if (!qos && 'trafficMode' in w)
        return false;
    if (probe) {
        const fields = ['schemaVersion', 'profileId', 'budgetKbpsPerDirection', 'perUe', 'requestedUplinkKbps',
            'requestedDownlinkKbps', 'requestedCombinedKbps', 'appliedUplinkKbps', 'appliedDownlinkKbps',
            'appliedCombinedKbps', 'capReason', 'comparisonFingerprint', 'qosEffectClaimed', 'workloadKind', 'probeKbpsPerDirection'];
        if (Object.keys(w).sort().join(',') !== fields.sort().join(',')
            || w.workloadKind !== 'diagnostic_probe' || !integer(w.probeKbpsPerDirection)
            || w.probeKbpsPerDirection < 1 || w.probeKbpsPerDirection > 100 || w.capReason !== null
            || !Array.isArray(w.perUe) || w.perUe.length < 1 || w.perUe.length > MAX_SCENARIO_UES)
            return false;
    }
    else if ((!qos && w.profileId !== 'scenario-offered-load-v1') || 'workloadKind' in w || 'probeKbpsPerDirection' in w)
        return false;
    if (w.schemaVersion !== 'cityverse.round-workload/v1'
        || w.budgetKbpsPerDirection !== budget || w.qosEffectClaimed !== false
        || !/^[a-f0-9]{64}$/.test(w.comparisonFingerprint) || !(w.capReason === null || typeof w.capReason === 'string')
        || !Array.isArray(w.perUe) || w.perUe.length > MAX_HISTORICAL_UES || new Set(w.perUe.map((r) => r?.supi)).size !== w.perUe.length
        || w.perUe.some((r) => !r || typeof r.supi !== 'string' || !/^imsi-[0-9]{15}$/.test(r.supi) || !['web_browsing', ...SCENARIO_EVENTS].includes(r.scenario)
            || (probe && Object.keys(r).sort().join(',') !== 'appliedKbps,requestedKbps,scenario,slice,supi')
            || r.slice !== profiles[r.scenario][0] || r.requestedKbps !== (probe ? w.probeKbpsPerDirection : profiles[r.scenario][1]) || !integer(r.requestedKbps) || !integer(r.appliedKbps)
            || (probe && r.appliedKbps !== w.probeKbpsPerDirection)
            || r.appliedKbps > r.requestedKbps || r.appliedKbps < 1))
        return false;
    const requested = w.perUe.reduce((sum, r) => sum + r.requestedKbps!, 0);
    const applied = w.perUe.reduce((sum, r) => sum + r.appliedKbps!, 0);
    const ordered = [...w.perUe].sort((a, b) => a.supi.localeCompare(b.supi)) as Array<{
        supi: string;
        requestedKbps: number;
        appliedKbps: number;
    }>;
    const shares = ordered.map((r) => qos || requested <= budget ? r.requestedKbps : Math.floor(r.requestedKbps * budget / requested));
    if (!qos && requested > budget) {
        const remainder = budget - shares.reduce((sum, n) => sum + n, 0);
        const priority = ordered.map((r, i) => ({ i, fraction: r.requestedKbps * budget % requested }))
            .sort((a, b) => b.fraction - a.fraction || a.i - b.i);
        priority.slice(0, remainder).forEach(({ i }) => { shares[i] += 1; });
    }
    if (ordered.some((r, i) => r.appliedKbps !== shares[i]))
        return false;
    return w.requestedUplinkKbps === requested && w.requestedDownlinkKbps === requested && w.requestedCombinedKbps === requested * 2
        && w.appliedUplinkKbps === applied && w.appliedDownlinkKbps === applied && w.appliedCombinedKbps === applied * 2
        && applied <= budget && ((applied < requested && w.capReason === 'requested_per_direction_load_exceeds_budget') || (applied === requested && w.capReason === null));
}
function workloadCanonical(w: RoundWorkload): string {
    return JSON.stringify([w.schemaVersion, w.profileId, w.budgetKbpsPerDirection,
        [...w.perUe].sort((a, b) => a.supi.localeCompare(b.supi)).map((r) => [r.supi, r.scenario, r.slice, r.requestedKbps, r.appliedKbps,
            r.requestedUplinkKbps, r.requestedDownlinkKbps, r.appliedUplinkKbps, r.appliedDownlinkKbps]),
        w.requestedUplinkKbps, w.requestedDownlinkKbps, w.requestedCombinedKbps,
        w.appliedUplinkKbps, w.appliedDownlinkKbps, w.appliedCombinedKbps, w.capReason, w.comparisonFingerprint, w.qosEffectClaimed,
        ...(w.profileId === 'controlled_qer_probe_v1' ? [w.workloadKind, w.probeKbpsPerDirection] : []),
        ...(['scenario-qos-offered-load-v1', 'scenario-service-offered-load-v1'].includes(w.profileId) ? [w.trafficMode, w.serviceContractRevision] : [])]);
}
export function parseScenarioAllocation(baseline: string, events: Record<ScenarioEvent, string>): ScenarioAllocation | null {
    const values = [baseline, ...SCENARIO_EVENTS.map((key) => events[key])];
    if (values.some((value) => !/^\d+$/.test(value)))
        return null;
    const counts = values.map(Number);
    if (counts.some((value) => !Number.isSafeInteger(value) || value > MAX_SCENARIO_UES))
        return null;
    const total = counts.reduce((sum, value) => sum + value, 0);
    if (total > MAX_SCENARIO_UES)
        return null;
    return { baselineUes: counts[0], events: Object.fromEntries(SCENARIO_EVENTS.map((key, index) => [key, counts[index + 1]])) as EventCounts, total };
}
export function freshTimestamp(value: string | null | undefined, now = Date.now()): boolean {
    if (!value)
        return false;
    const age = now - Date.parse(value);
    return Number.isFinite(age) && age >= -1000 && age <= 15000;
}
export function acceptScenarioSnapshot(current: ScenarioSnapshot | null, incoming: ScenarioSnapshot): ScenarioSnapshot {
    if (current && incoming.sequence === current.sequence && incoming.roundId === current.roundId
        && incoming.generation === current.generation && incoming.policyRevision === current.policyRevision
        && incoming.lifecycle && incoming.lifecycle.revision > (current.lifecycle?.revision ?? -1)) {
        const progress = incoming.agentRuntime && incoming.agentRuntime.updatedAt > (current.agentRuntime?.updatedAt ?? 0);
        return { ...current, lifecycle: incoming.lifecycle, ...(progress ? { agentRuntime: incoming.agentRuntime } : {}) };
    }
    // Sequence is monotonic across rounds; an older poll cannot replace a POST result.
    if (current && incoming.sequence === current.sequence && current.roundId === incoming.roundId
        && current.generation === incoming.generation && current.policyRevision === incoming.policyRevision
        && incoming.agentRuntime && incoming.agentRuntime.updatedAt > (current.agentRuntime?.updatedAt ?? 0)) {
        // Agent progress has its own clock; it cannot renew native evidence or its sequence.
        return { ...current, agentRuntime: incoming.agentRuntime };
    }
    if (current && incoming.sequence <= current.sequence)
        return current;
    if (current?.roundId === incoming.roundId && current.generation !== null && (incoming.generation === null || incoming.generation < current.generation))
        return current;
    if (current?.roundId === incoming.roundId && incoming.policyRevision < current.policyRevision)
        return current;
    return incoming;
}
export function roundIsReleased(snapshot: ScenarioSnapshot | null): boolean {
    return snapshot?.stage === 'idle' || (snapshot?.stage === 'completed' && snapshot.cleanupStatus === 'verified');
}
const BINDING_KEYS = ['scenario', 'supi', 'sessionId', 'podUid', 'ueIp', 'tun', 'upf'] as const;
const SCENARIO_SLICE = SCENARIO_SLICES;
function deviceBinding(binding: UeBinding, generation: number | null): boolean {
    return Boolean(binding && ['web_browsing', ...SCENARIO_EVENTS].includes(binding.scenario)
        && /^imsi-[0-9]{15}$/.test(binding.supi) && typeof binding.podUid === 'string' && binding.podUid.length > 0
        && Number.isSafeInteger(binding.generation) && binding.generation === generation
        && binding.slice === SCENARIO_SLICE[binding.scenario]
        && ['sessionId', 'ueIp', 'tun', 'upf'].every(key => !(key in binding)));
}
function bindingKey(binding: UeBinding): string | null {
    const nativeSession = Number.isSafeInteger(binding.sessionId) && Number(binding.sessionId) >= 1 && Number(binding.sessionId) <= 255;
    return ['web_browsing', ...SCENARIO_EVENTS].includes(binding.scenario)
        && BINDING_KEYS.every(key => key === 'sessionId' ? nativeSession || (typeof binding[key] === 'string' && binding[key]!.length > 0)
            : typeof binding[key] === 'string' && binding[key]!.length > 0)
        ? JSON.stringify(BINDING_KEYS.map(key => key === 'sessionId' ? String(binding[key])
            : key === 'upf' && ['embb', 'urllc', 'mmtc', 'v2x'].includes(binding[key]!) ? `upf-${binding[key]}` : binding[key])) : null;
}
function hasV2SessionPublication(snapshot: ScenarioSnapshot | null): boolean {
    return Boolean(snapshot && ('nativeSessionBindings' in snapshot || 'perSession' in snapshot || 'devices' in snapshot
        || ('schemaVersion' in snapshot && snapshot.schemaVersion === 'cityverse.current-state/v2')));
}
/** Evidence coverage, not a throughput/SLA verdict. Missing rows retain the requested denominator. */
export function receiverEvidenceCoverage(snapshot: ScenarioSnapshot | null, now = Date.now()) {
    const expected = snapshot?.allocation.total ?? 0;
    const empty = { expected, sessionBindings: 0, uplink: 0, downlink: 0, duplex: 0,
        missingSessionBindings: expected, missingUplink: expected, missingDownlink: expected, status: 'unknown' as 'complete' | 'unknown' };
    // A V2 session view requires its own validated proof. Never downgrade an
    // incomplete/invalid V2 publication to legacy perUe ownership evidence.
    if (!snapshot?.roundId || !Number.isSafeInteger(expected) || expected < 1
        || !freshTimestamp(snapshot.observedAt, now)
        || !['measuring_baseline', 'applying_policy', 'verifying_policy', 'measuring_result', 'results_ready'].includes(snapshot.stage)) return empty;
    const native = hasV2SessionPublication(snapshot) ? validatedNativeBindings(snapshot, now) : undefined;
    if (native === null) return empty;
    const bindings = native ?? snapshot.perUe;
    const nativeOwners = new Map(native?.map(row => [row.supi, row]));
    const owners = new Map<string, string>(), seenOwners = new Set<string>();
    for (const binding of bindings) {
        if (seenOwners.has(binding.supi)) return empty;
        seenOwners.add(binding.supi);
        const key = bindingKey(binding);
        if (key) owners.set(binding.supi, key);
    }
    if (owners.size > expected) return empty;
    const occurrences = new Map<string, number>();
    for (const row of snapshot.measurements) occurrences.set(row.supi, (occurrences.get(row.supi) ?? 0) + 1);
    let uplink = 0, downlink = 0, duplex = 0;
    for (const row of snapshot.measurements) {
        if (occurrences.get(row.supi) !== 1 || !owners.has(row.supi) || owners.get(row.supi) !== bindingKey(row)
            || row.roundId !== snapshot.roundId || row.generation !== snapshot.generation
            || row.policyRevision !== snapshot.policyRevision || row.source !== 'receiver' || !freshTimestamp(row.observedAt, now)) continue;
        // Zero is a measured rate; null is missing. Coverage never claims SLA success.
        const measured = (value: number | null) => typeof value === 'number' && Number.isFinite(value) && value >= 0;
        const directions = native ? nativeReceiverDirections(row, nativeOwners.get(row.supi)!, now) : null;
        const ul = directions?.ul ?? measured(row.receivedUplinkMbps), dl = directions?.dl ?? measured(row.receivedDownlinkMbps);
        uplink += Number(ul); downlink += Number(dl); duplex += Number(ul && dl);
    }
    return { expected, sessionBindings: owners.size, uplink, downlink, duplex,
        missingSessionBindings: expected - owners.size, missingUplink: expected - uplink, missingDownlink: expected - downlink,
        status: duplex === expected && owners.size === expected ? 'complete' as const : 'unknown' as const };
}
export function verifiedReceiverMeasurements(snapshot: ScenarioSnapshot | null, now = Date.now()): ReceiverMeasurement[] {
    if (!snapshot?.roundId || !freshTimestamp(snapshot.observedAt, now)
        || !['measuring_baseline', 'applying_policy', 'verifying_policy', 'measuring_result', 'results_ready'].includes(snapshot.stage))
        return [];
    const native = hasV2SessionPublication(snapshot) ? validatedNativeBindings(snapshot, now) : undefined;
    if (native === null) return [];
    const nativeOwners = new Map(native?.map(row => [row.supi, row]));
    const owners = new Map<string, UeBinding>();
    for (const binding of native ?? snapshot.perUe) {
        const key = bindingKey(binding);
        if (!key || owners.has(binding.supi))
            return []; // ambiguous ownership is not evidence
        owners.set(binding.supi, binding);
    }
    const seen = new Set<string>();
    const result: ReceiverMeasurement[] = [];
    for (const row of snapshot.measurements) {
        const owner = owners.get(row.supi);
        if (!owner || bindingKey(owner) !== bindingKey(row) || seen.has(row.supi))
            return [];
        seen.add(row.supi);
        if (row.roundId !== snapshot.roundId || row.generation !== snapshot.generation || row.policyRevision !== snapshot.policyRevision
            || row.source !== 'receiver' || !freshTimestamp(row.observedAt, now))
            continue;
        if (native) {
            const directions = nativeReceiverDirections(row, nativeOwners.get(row.supi)!, now);
            if (!directions.ul || !directions.dl) continue;
        }
        const rates = [row.receivedUplinkMbps, row.receivedDownlinkMbps];
        if (rates.some((value) => value !== null && (typeof value !== 'number' || !Number.isFinite(value) || value < 0)))
            continue;
        if (!rates.some((value) => value !== null && value > 0))
            continue;
        result.push(row);
    }
    return result;
}
export function parseScenarioSnapshot(value: unknown): ScenarioSnapshot {
    let s = value as ScenarioSnapshot;
    if (s && typeof s === 'object' && Array.isArray(s.actions)) {
        // Older derived traffic aggregates omitted non-actuator classification
        // flags. Keep historical evidence immutable and never infer an ACK/effect.
        s = { ...s, actions: s.actions.map((action) => action?.kind === 'traffic_aggregate'
                ? { ...action, ...(action.requested === undefined ? { requested: false } : {}),
                    ...(action.installed === undefined ? { installed: false } : {}) }
                : action) };
    }
    if (!s || typeof s !== 'object' || !(s.roundId === null || typeof s.roundId === 'string')
        || (s.viewSchema !== undefined && s.viewSchema !== 'cityverse.round-summary/v1')
        || ![s.sequence, s.policyRevision].every((n) => Number.isSafeInteger(n) && n >= 0)
        || !(s.generation === null ? ['idle', 'preflight', 'allocating', 'cleaning', 'completed', 'verification_unavailable'].includes(s.stage) : Number.isSafeInteger(s.generation) && s.generation >= 0)
        || typeof s.stage !== 'string' || typeof s.cleanupStatus !== 'string'
        || !(s.observedAt === null || (typeof s.observedAt === 'string' && Number.isFinite(Date.parse(s.observedAt))))
        || !s.allocation || !s.allocation.events || !s.counts || !s.capabilities
        || ![null, 'passed', 'failed', 'cancelled'].includes(s.executionOutcome)
        || !Array.isArray(s.perUe) || !Array.isArray(s.measurements) || !Array.isArray(s.actions)
        || s.actions.some((action) => !action || typeof action.id !== 'string' || (action.kind !== undefined && typeof action.kind !== 'string') || ['requested', 'accepted', 'installed', 'effectVerified'].some((key) => typeof action[key as keyof typeof action] !== 'boolean'))
        || !Array.isArray(s.errors) || s.errors.some((error) => typeof error !== 'string'))
        throw new Error('Invalid scenario snapshot');
    const life = s.lifecycle;
    if (life && (life.schemaVersion !== 'cityverse.scenario-lifecycle/v1' || life.maximumCycles !== 5
        || !Number.isSafeInteger(life.revision) || life.revision < 0
        || !['idle', 'preparing', 'running', 'stopping', 'stopped', 'failed'].includes(life.scenarioState)
        || !['pending', 'running', 'completed', 'failed', 'cancelled'].includes(life.evaluationState)
        || !['reserved', 'held', 'releasing', 'released', 'unknown'].includes(life.resourceState)
        || ![life.cyclesAttempted, life.cyclesCompleted].every((n) => Number.isSafeInteger(n) && n >= 0 && n <= 5)
        || typeof life.canSubmitEvaluation !== 'boolean' || !['start', 'queue', 'blocked'].includes(life.submissionMode)))
        throw new Error('Invalid scenario lifecycle');
    const rawCounts = [s.allocation.baselineUes, ...SCENARIO_EVENTS.map((key) => s.allocation.events[key])];
    if (!rawCounts.every((n) => Number.isSafeInteger(n) && n >= 0 && n <= MAX_HISTORICAL_UES)
        || rawCounts.reduce((sum, n) => sum + n, 0) !== s.allocation.total || s.allocation.total > MAX_HISTORICAL_UES
        || ['allocated', 'registered', 'pduEstablished', 'trafficVerified'].some((key) => {
            const n = s.counts[key as keyof typeof s.counts];
            return n !== null && (!Number.isSafeInteger(n) || n < 0 || n > MAX_HISTORICAL_UES);
        }) || (s.generation === null && (s.perUe.length > 0 || s.measurements.length > 0))
        || s.perUe.some((row) => !row || (!bindingKey(row) && !deviceBinding(row, s.generation)))
        || s.measurements.some((row) => !row || !bindingKey(row)))
        throw new Error('Invalid scenario identities or counts');
    if (Object.keys(s.capabilities).some((k) => ['applicationMaxUes', 'verifiedMaxUes', 'verifiedMaxUesScope', 'candidateMaxUes', 'admissionMaxUes', 'commissioningEnabled', 'commissioningProfile'].includes(k))
        && admissionLimit(s.capabilities) === null)
        throw new Error('Invalid commissioning admission capability');
    if (s.requestedSliceUes !== undefined) {
        const expected = { embb: s.allocation.baselineUes + s.allocation.events.video_streaming,
            urllc: s.allocation.events.realtime_control, v2x: s.allocation.events.connected_traffic, mmtc: s.allocation.events.factory_iot };
        if (Object.keys(s.requestedSliceUes).sort().join(',') !== Object.keys(expected).sort().join(',')
            || Object.entries(expected).some(([key, n]) => s.requestedSliceUes![key as keyof typeof expected] !== n))
            throw new Error('Invalid requested slice counts');
    }
    if (s.trafficCounts !== undefined) {
        const t = s.trafficCounts;
        if (!t || t.scope !== 'selected-round-ues' || t.requested !== s.allocation.total || t.selected !== s.perUe.length
            || !['last-receiver-window', 'unavailable'].includes(t.measurementScope)
            || [t.registered, t.pduEstablished, t.connectivityVerified, t.observedTransmitting].some((n) => n !== null && (!Number.isSafeInteger(n) || n < 0 || n > t.requested))
            || (t.measurementScope === 'unavailable' && (t.observedTransmitting !== null || t.observedAt !== null))
            || (t.measurementScope === 'last-receiver-window' && (t.observedTransmitting === null || !t.observedAt || !Number.isFinite(Date.parse(t.observedAt)))))
            throw new Error('Invalid traffic participation counts');
    }
    if (s.actions.some((a) => a.workload !== undefined && (a.kind !== 'traffic_window'
        || (!validWorkload(a.workload) && !validSessionWorkloadSummary(a.workload)))))
        throw new Error('Invalid fixed workload metadata');
    if (s.agentRuntime !== undefined) {
        const p = s.agentRuntime;
        if (!p || p.roundId !== s.roundId || p.generation !== s.generation
            || !['plan', 'precheck', 'apply', 'postcheck', 'restore', 'restoration', 'observe', 'learn', 'cleanup', 'completed', 'failed'].includes(p.phase)
            || !['started', 'completed', 'failed'].includes(p.status)
            || !Number.isFinite(p.updatedAt) || p.updatedAt <= 0
            || (p.role !== undefined && !['planner', 'executor', 'validator'].includes(p.role))
            || (p.cycle !== undefined && (!Number.isSafeInteger(p.cycle) || p.cycle < 0 || p.cycle > 10000))
            || [p.tool, p.model, p.response_id, p.code].some((v) => v !== undefined && (typeof v !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(v)))
            || Object.keys(p).some((k) => !['roundId', 'generation', 'phase', 'status', 'updatedAt', 'role', 'tool', 'model', 'response_id', 'cycle', 'code'].includes(k)))
            throw new Error('Invalid AI progress');
    }
    if (s.trafficMode !== undefined && !['functional', 'qos'].includes(s.trafficMode))
        throw new Error('Invalid traffic mode');
    if (s.serviceRequirements !== undefined && (s.trafficMode !== 'qos' || !validServiceRequirements(s.serviceRequirements, s.allocation)
        || !s.resolvedRequirements || !/^[a-f0-9]{64}$/.test(s.resolvedRequirements.revision) || s.resolvedRequirements.source !== 'user-request'))
        throw new Error('Invalid frozen service requirements');
    if (s.resolvedRequirements !== undefined && s.serviceRequirements === undefined)
        throw new Error('Missing raw service requirements');
    if (s.sla !== undefined && !validSlaAssessment(s.sla))
        throw new Error('Invalid SLA assessment');
    if (s.sla && 'revision' in s.sla && s.sla.revision !== s.resolvedRequirements?.revision)
        throw new Error('SLA revision differs from frozen requirements');
    if (s.performance !== undefined && !validCapacityPerformance(s))
        throw new Error('Invalid capacity performance verdict');
    const plans = s.actions.flatMap((a) => validWorkload(a.workload) ? [a.workload] : []);
    const summaries = s.actions.flatMap(a => validSessionWorkloadSummary(a.workload) ? [a.workload] : []);
    if (summaries.length && (plans.length || s.trafficMode !== 'qos'
        || summaries.some(w => w.comparisonFingerprint !== summaries[0].comparisonFingerprint)))
        throw new Error('V2 workload summary differs');
    if (plans.some((plan) => (['scenario-qos-offered-load-v1', 'scenario-service-offered-load-v1'].includes(plan.profileId)) !== (s.trafficMode === 'qos')
        || (plan.profileId === 'scenario-service-offered-load-v1' ? plan.serviceContractRevision !== s.resolvedRequirements?.revision : s.serviceRequirements !== undefined)
        || plan.perUe.length !== s.perUe.length || plan.perUe.some((row) => !s.perUe.some((owner) => owner.supi === row.supi && owner.scenario === row.scenario && owner.upf === `upf-${row.slice}`)))
        || plans.some((plan) => workloadCanonical(plan) !== workloadCanonical(plans[0])))
        throw new Error('Workload ownership or frozen plan differs');
    if (s.capabilities.scopedVerifiedTrafficProfiles !== undefined)
        s.capabilities.scopedVerifiedTrafficProfiles = decodeScopedTrafficCapabilities(s.capabilities.scopedVerifiedTrafficProfiles);
    return s;
}
export class ScenarioHttpError extends Error {
    readonly definitive: boolean;
    readonly code: 'ROUND_CONFLICT' | 'TRAFFIC_GENERATOR_LIMIT_EXCEEDED' | 'TRAFFIC_GENERATOR_LIMIT_UNAVAILABLE' | null;
    readonly baselineUnavailable: boolean;
    constructor(readonly status: number, body?: unknown) {
        super(`Scenario API unavailable: HTTP ${status}`);
        const data = body && typeof body === 'object' ? body as Record<string, unknown> : null;
        this.code = status === 409 && data?.error === 'ROUND_CONFLICT' ? 'ROUND_CONFLICT'
            : status === 503 && data?.error === 'TRAFFIC_GENERATOR_LIMIT_EXCEEDED' ? 'TRAFFIC_GENERATOR_LIMIT_EXCEEDED'
                : status === 503 && data?.error === 'TRAFFIC_GENERATOR_LIMIT_UNAVAILABLE' ? 'TRAFFIC_GENERATOR_LIMIT_UNAVAILABLE' : null;
        this.baselineUnavailable = this.code === 'ROUND_CONFLICT' && data?.detail === 'Fresh known-scope resource baseline is unavailable';
        this.definitive = [400, 401, 403, 404, 409, 422, 503].includes(status);
    }
}
export function scenarioErrorText(cause: unknown): string {
    if (cause == null)
        return '';
    if (!(cause instanceof ScenarioHttpError))
        return String(cause);
    if (cause.code === 'TRAFFIC_GENERATOR_LIMIT_EXCEEDED')
        return 'QoS 實驗負載超過已設定的流量產生器上限；請求未接受，沒有縮減發送量。';
    if (cause.code === 'TRAFFIC_GENERATOR_LIMIT_UNAVAILABLE')
        return 'QoS 流量產生器上限設定不可用；請求未接受。';
    if (cause.baselineUnavailable)
        return '目前無法取得新的已知範圍資源基準，請求未被接受（HTTP 409）。';
    if (cause.code === 'ROUND_CONFLICT')
        return '輪次狀態衝突，請求未被接受（HTTP 409）。';
    return `情境 API 無法完成請求：HTTP ${cause.status}`;
}
export async function scenarioRead<T>(path: string, signal?: AbortSignal): Promise<T> {
    return scenarioRequest(path, undefined, signal) as Promise<T>;
}
async function scenarioRequest(path: string, body?: unknown, signal?: AbortSignal): Promise<unknown> {
    const response = await fetch(`${apiBaseUrl()}/api/onprem/scenario-rounds${path}`, {
        method: body ? 'POST' : 'GET', headers: { ...sessionHeaders(), ...(body ? { 'Content-Type': 'application/json' } : {}) },
        ...(body ? { body: JSON.stringify(body) } : {}), signal,
    });
    if (!response.ok) {
        // Store only allowlisted classifications, never arbitrary server detail.
        let body: unknown;
        try {
            const raw = await response.text();
            if (raw.length <= 4096)
                body = JSON.parse(raw);
        }
        catch { /* HTTP classification still definitive */ }
        throw new ScenarioHttpError(response.status, body);
    }
    return response.json();
}
async function scenarioFetch(path: string, body?: unknown, signal?: AbortSignal): Promise<ScenarioSnapshot> {
    return parseScenarioSnapshot(await scenarioRequest(path, body, signal));
}
export const getScenarioRound = (signal?: AbortSignal) => scenarioFetch('/current?view=summary', undefined, signal);
