import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ScenarioAdapterProvider, type ScenarioAdapter } from '../../components/OnpremRuntime/ScenarioContext';
import { OnpremPolicyPanel, OnpremRoundEvidence } from '../../components/OnpremRuntime/ScenarioPanels';
import { acceptScenarioSnapshot, admissionLimit, allocationAdmitted, freshTimestamp, parseScenarioAllocation, parseScenarioSnapshot, roundIsReleased, SCENARIO_EVENTS, verifiedReceiverMeasurements, type ScenarioSnapshot } from '../onpremScenarios';
it('parses population admission independently of the native start and formal AI gates', () => {
    const value = { ...snapshot(), capabilities: { applicationMaxUes: 100, verifiedMaxUes: 0,
            verifiedMaxUesScope: 'scenario-offered-load-v1' as const, candidateMaxUes: 100, admissionMaxUes: 100,
            commissioningEnabled: false, commissioningProfile: null, capacityStatus: 'unverified_ai_trial',
            start: false, staticPolicy: false, aiPolicy: false } };
    const current = parseScenarioSnapshot(value);
    expect(admissionLimit(current.capabilities)).toBe(100);
    expect(admissionLimit(parseScenarioSnapshot({ ...value, capabilities: { ...value.capabilities, start: true } }).capabilities)).toBe(100);
});
it('renders bounded diagnostics, never policy booleans', () => {
    const s = snapshot();
    s.actions = [
        { id: 'execution', kind: 'execution_failure', code: 'ue.registration_rejected', stage: 'registering', observedAt: new Date(now).toISOString(), requested: false, accepted: false, installed: false, effectVerified: false },
        { id: 'traffic', kind: 'traffic_failure', code: 'batch.window_unverified', phase: 'final_freshness', requestedAt: new Date(now).toISOString(), requested: true, accepted: false, installed: false, effectVerified: false },
        { id: 'unknown-diagnostic', kind: 'traffic_failure', code: 'raw-secret', phase: 'raw-secret', requestedAt: 'raw-secret', requested: false, accepted: false, installed: false, effectVerified: false },
    ];
    const html = renderToStaticMarkup(<ScenarioAdapterProvider value={adapter(s)}><OnpremPolicyPanel /></ScenarioAdapterProvider>);
    const cards = html.match(/<article[\s\S]*?<\/article>/g)!.join('');
    expect(cards).toContain('ue.registration_rejected');
    expect(cards).toContain('registering');
    expect(cards).toContain('batch.window_unverified');
    expect(cards).toContain('final_freshness');
    expect(cards).toContain(new Date(now).toISOString());
    expect(cards).toContain('執行未完成診斷（非政策）');
    expect(cards).not.toContain('raw-secret');
    expect(cards).not.toContain('effectVerified');
    expect(cards).not.toContain('Unclassified');
    expect(cards).not.toContain('application-policy-audit');
});
const now = Date.parse('2026-09-28T02:00:00Z');
const empty = { video_streaming: 0,  connected_traffic: 0, realtime_control: 0, factory_iot: 0 };
const draft = { video_streaming: '0',  connected_traffic: '0', realtime_control: '0', factory_iot: '0' };
function snapshot(): ScenarioSnapshot {
    const binding = { scenario: 'web_browsing' as const, supi: 'imsi-208930000000001', sessionId: '1', ueIp: '10.101.0.1', tun: 'uesimtun0', upf: 'upf-embb', podUid: 'uid1' };
    return { roundId: 'round1', generation: 1, sequence: 1, policyRevision: 0, observedAt: new Date(now).toISOString(), stage: 'measuring_baseline', executionOutcome: null, cleanupStatus: 'pending', allocation: { baselineUes: 1, events: empty, total: 1 }, counts: { allocated: 1, registered: 1, pduEstablished: 1, trafficVerified: 1 }, capabilities: { start: true, staticPolicy: false, aiPolicy: false }, actions: [], errors: [], perUe: [binding], measurements: [{ ...binding, roundId: 'round1', generation: 1, policyRevision: 0, source: 'receiver', observedAt: new Date(now).toISOString(), receivedUplinkMbps: 2, receivedDownlinkMbps: 3, rttMs: null }] };
}
function adapter(s: ScenarioSnapshot | null = snapshot()): ScenarioAdapter {
    return { snapshot: s, error: '', pending: false, uncertain: false, now, fresh: true };
}
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });
describe('historical derived traffic aggregates', () => {
    it('reads old aggregate classification without mutating the historical input', () => {
        const old = { id: 'aggregate', kind: 'traffic_aggregate', accepted: true, effectVerified: true };
        const result = parseScenarioSnapshot({ ...snapshot(), actions: [old] });
        expect(result.actions[0]).toEqual({ ...old, requested: false, installed: false });
        expect(old).not.toHaveProperty('requested');
        expect(old).not.toHaveProperty('installed');
    });
    it.each([
        { id: 'a', kind: 'policy_apply', accepted: true, effectVerified: true },
        { id: 'a', kind: 'traffic_aggregate', accepted: true },
        { id: 'a', kind: 'traffic_aggregate', accepted: true, effectVerified: true, requested: null },
    ])('still rejects missing observations or malformed classifications: %j', (action) => {
        expect(() => parseScenarioSnapshot({ ...snapshot(), actions: [action] })).toThrow('Invalid scenario snapshot');
    });
});
describe('commissioning and verified AI admission', () => {
    const commissioning = { applicationMaxUes: 100, verifiedMaxUes: 0,
        verifiedMaxUesScope: 'scenario-offered-load-v1' as const,
        commissioningEnabled: true, candidateMaxUes: 100, admissionMaxUes: 100,
        commissioningProfile: 'scenario-offered-load-v1', staticPolicy: false, aiPolicy: false };
    const allocation = { baselineUes: 100, events: empty, total: 100 };
    it('admits the explicit unverified candidate while rejecting a verified-one claim', () => {
        expect(admissionLimit(commissioning)).toBe(100);
        expect(allocationAdmitted(commissioning, allocation)).toBe(true);
        expect(admissionLimit({ ...commissioning, verifiedMaxUes: 1 })).toBeNull();
        expect(allocationAdmitted({ ...commissioning, verifiedMaxUes: 1 }, allocation)).toBe(false);
    });
    it('keeps verified capacity separate from the legacy AI policy flag', () => {
        const formal = { ...commissioning, commissioningEnabled: false,
            candidateMaxUes: null, commissioningProfile: null,
            verifiedMaxUes: 100, aiPolicy: true };
        expect(admissionLimit(formal)).toBe(100);
        expect(admissionLimit({ ...formal, verifiedMaxUes: 1 })).toBeNull();
        expect(admissionLimit({ ...formal, aiPolicy: false })).toBe(100);
        expect(admissionLimit({ ...formal, commissioningEnabled: true })).toBeNull();
    });
});
describe('historical capacity verdict is separate from execution success', () => {
    function failedCapacity(): ScenarioSnapshot {
        const s = { ...snapshot(), stage: 'completed', executionOutcome: 'passed' as const, cleanupStatus: 'verified' };
        return { ...s, performance: { status: 'failed', criteriaId: 'capacity-delivery-v1',
                scope: 'historical-bounded-offered-load-delivery', reason: 'delivery_criteria_not_met',
                flowCount: 4, failedFlowCount: 1, failedFlows: [{ stage: 'measuring_result', supi: s.perUe[0].supi,
                        direction: 'dl', appliedKbps: 400, offeredAppliedRatio: 1, receivedAppliedRatio: 0.85,
                        conservativeUniqueAppliedRatio: 0.85, receiverLossPercent: 15 }],
                provenance: { roundId: s.roundId, generation: s.generation, sequence: s.sequence, observedAt: s.observedAt,
                    source: 'raw-native-iperf3-reports', historical: true, currentNetworkStateClaimed: false },
                promotionEligible: false, qosEffectClaimed: false } };
    }
    it('shows failed delivery despite passed execution', () => {
        const s = parseScenarioSnapshot(failedCapacity());
        const html = renderToStaticMarkup(<OnpremRoundEvidence adapter={adapter(s)}/>);
        expect(html).toContain('data-execution-outcome="passed"');
        expect(html).toContain('data-performance-status="failed"');
        expect(html).toContain('未達標');
        expect(html).toContain('1 / 4');
        expect(html).toContain('本輪歷史量測');
    });
    it('does not infer a capacity pass from missing evidence', () => {
        const html = renderToStaticMarkup(<OnpremRoundEvidence adapter={adapter(snapshot())}/>);
        expect(html).toContain('data-performance-status="unavailable"');
    });
    it.each(['results_ready', 'cleaning', 'completed'])('shows historical quality in %s without admitting stale current traffic', (stage) => {

        const s = failedCapacity();
        s.stage = stage;
        s.cleanupStatus = stage === 'completed' ? 'verified' : 'blocked';
        s.executionOutcome = 'failed';
        s.observedAt = '2000-01-01T00:00:00Z';
        s.performance!.provenance.observedAt = s.observedAt;
        const parsed = parseScenarioSnapshot(s);
        const html = renderToStaticMarkup(<OnpremRoundEvidence adapter={adapter(parsed)}/>);
        expect(html).toContain('data-performance-status="failed"');
        expect(html).toContain('data-execution-outcome="failed"');
        expect(html).toContain("未達標");
        expect(freshTimestamp(s.observedAt, now)).toBe(false);
        expect(s.capabilities.staticPolicy).toBe(false);
        expect(s.capabilities.aiPolicy).toBe(false);
        expect(s.performance!.promotionEligible).toBe(false);
    });
    it.each(['results_ready', 'cleaning'])('accepts complete passed historical verdict before release in %s', (stage) => {

        const s = failedCapacity();
        s.stage = stage;
        s.cleanupStatus = 'not_started';
        Object.assign(s.performance!, { status: 'passed', reason: 'delivery_criteria_met', failedFlowCount: 0, failedFlows: [] });
        const html = renderToStaticMarkup(<OnpremRoundEvidence adapter={adapter(parseScenarioSnapshot(s))}/>);
        expect(html).toContain("達標");
        expect(html).toContain('data-cleanup-status="not_started"');
        expect(html).toContain('此判定不代表目前網路狀態、容量認證或 QoS 政策效果');
    });
    it.each(['measurements_not_ready', 'round_not_completed', 'evidence_invalid_or_incomplete'])('labels pending versus unavailable: %s', (reason) => {

        const s = failedCapacity();
        Object.assign(s.performance!, { status: 'unavailable', reason, flowCount: 0, failedFlowCount: 0, failedFlows: [] });
        const html = renderToStaticMarkup(<OnpremRoundEvidence adapter={adapter(parseScenarioSnapshot(s))}/>);
        expect(html).toContain(reason === 'evidence_invalid_or_incomplete' ? "量測證據不可用" : "等待完整量測");
        expect(html).not.toContain('data-performance-status="passed"');
    });
    it.each(['sequence', 'observedAt', 'failedFlowCount', 'nan', 'inflight', 'promotion', 'foreignUe', 'duplicate'])('rejects invalid verdict: %s', (mutation) => {
        const s = failedCapacity();
        const p = s.performance!;
        if (mutation === 'sequence')
            p.provenance.sequence += 1;
        if (mutation === 'observedAt')
            p.provenance.observedAt = null;
        if (mutation === 'failedFlowCount')
            p.failedFlowCount = 0;
        if (mutation === 'nan')
            p.failedFlows[0].receiverLossPercent = NaN;
        if (mutation === 'inflight')
            s.stage = 'measuring_result';
        if (mutation === 'promotion')
            Object.assign(p, { promotionEligible: true });
        if (mutation === 'foreignUe')
            p.failedFlows[0].supi = 'imsi-208930999999999';
        if (mutation === 'duplicate') {
            p.failedFlows.push({ ...p.failedFlows[0] });
            p.failedFlowCount = 2;
        }
        expect(() => parseScenarioSnapshot(s)).toThrow('Invalid capacity performance verdict');
    });
});
describe('actual shared UE budget', () => {
    it.each([0, 1, 50, 99, 100])('accepts baseline-only draft %i without phantom event allocation', (n) => expect(parseScenarioAllocation(String(n), draft)?.total).toBe(n));
    it('includes all five service profiles in the same total', () => {
        expect(parseScenarioAllocation('20', { video_streaming: '10',  connected_traffic: '20', realtime_control: '20', factory_iot: '30' })?.total).toBe(100);
        expect(parseScenarioAllocation('101', { video_streaming: '80',  connected_traffic: '80', realtime_control: '80', factory_iot: '80' })).toBeNull();
    });
    it.each(['101', '501', '-1', '1.5', 'NaN', 'Infinity', '', '1e1'])('rejects invalid counts %s', (value) => expect(parseScenarioAllocation(value, draft)).toBeNull());
    it('supports all32 event combinations within the budget', () => {
        for (let mask = 0; mask < 32; mask++) {
            const events = Object.fromEntries(SCENARIO_EVENTS.map((key, i) => [key, mask & (1 << i) ? '5' : '0'])) as typeof draft;
            expect(parseScenarioAllocation('5', events)?.total).toBeLessThanOrEqual(30);
        }
    });
});
describe('snapshot freshness and identity', () => {
    it.each(['cleaning', 'completed'])('accepts serialized preallocation %s without inventing UE identity', (stage) => {
        const cleanup = JSON.parse('{"roundId":"cancelled-before-allocation","requestId":"cancelled-request","generation":null,"policyRevision":0,"sequence":12,"observedAt":null,"stage":"cleaning","executionOutcome":"cancelled","cleanupStatus":"pending","allocation":{"baselineUes":0,"events":{"video_streaming":0,"realtime_control":0,"connected_traffic":0,"realtime_control":0,"factory_iot":0},"total":0},"counts":{"allocated":null,"registered":null,"pduEstablished":null,"trafficVerified":null},"perUe":[],"actions":[],"measurements":[],"errors":[],"capabilities":{"start":false,"staticPolicy":false,"aiPolicy":false}}');
        cleanup.stage = stage;
        cleanup.cleanupStatus = stage === 'completed' ? 'verified' : 'pending';
        const parsed = parseScenarioSnapshot(cleanup);
        expect(parsed.generation).toBeNull();
        expect(parsed.executionOutcome).toBe('cancelled');
        expect(roundIsReleased(parsed)).toBe(stage === 'completed');
        expect(verifiedReceiverMeasurements(parsed, now)).toEqual([]);
        expect(() => parseScenarioSnapshot({ ...cleanup, perUe: snapshot().perUe })).toThrow();
        expect(() => parseScenarioSnapshot({ ...cleanup, measurements: snapshot().measurements })).toThrow();
    });
    it('accepts actual serialized backend idle shape without inventing generation or observations', () => {
        const idle = JSON.parse('{"roundId":null,"generation":null,"policyRevision":0,"sequence":0,"observedAt":null,"stage":"idle","executionOutcome":null,"cleanupStatus":"not_started","allocation":{"baselineUes":0,"events":{"video_streaming":0,"realtime_control":0,"connected_traffic":0,"realtime_control":0,"factory_iot":0},"total":0},"counts":{"allocated":null,"registered":null,"pduEstablished":null,"trafficVerified":null},"perUe":[],"actions":[],"measurements":[],"errors":[],"capabilities":{"start":false,"staticPolicy":false,"aiPolicy":false,"reason":"Native scenario traffic/policy capability verification is incomplete"}}');
        expect(parseScenarioSnapshot(idle).generation).toBeNull();
        expect(verifiedReceiverMeasurements(idle, now)).toEqual([]);
        expect(() => parseScenarioSnapshot({ ...snapshot(), generation: null })).toThrow();
    });
    it('accepts new rounds with global sequence and rejects prior idle replay', () => {
        const current = { ...snapshot(), roundId: 'round1', sequence: 80, stage: 'completed', cleanupStatus: 'verified' };
        const incoming = { ...snapshot(), roundId: 'round2', sequence: 81, generation: 2 };
        expect(acceptScenarioSnapshot(current, incoming)).toBe(incoming);
        expect(acceptScenarioSnapshot(incoming, { ...snapshot(), roundId: null, generation: null, sequence: 0, stage: 'idle' })).toBe(incoming);
    });
    it('accepts actual ledger reserve before UE generation is assigned', () => {
        const reserved = JSON.parse('{"roundId":"round2","requestId":"request2","generation":null,"policyRevision":0,"sequence":81,"observedAt":null,"stage":"preflight","executionOutcome":null,"cleanupStatus":"not_started","allocation":{"baselineUes":1,"events":{"video_streaming":0,"realtime_control":0,"connected_traffic":0,"realtime_control":0,"factory_iot":0},"total":1},"counts":{"allocated":null,"registered":null,"pduEstablished":null,"trafficVerified":null},"perUe":[],"actions":[],"measurements":[],"errors":[],"capabilities":{"start":false,"staticPolicy":false,"aiPolicy":false}}');
        const parsed = parseScenarioSnapshot(reserved);
        const old = { ...snapshot(), sequence: 80, stage: 'completed', cleanupStatus: 'verified' };
        expect(acceptScenarioSnapshot(old, parsed)).toBe(parsed);
        expect(parsed.generation).toBeNull();
        expect(verifiedReceiverMeasurements(parsed, now)).toEqual([]);
        expect(parseScenarioSnapshot({ ...reserved, stage: 'allocating' }).generation).toBeNull();
        expect(() => parseScenarioSnapshot({ ...reserved, perUe: snapshot().perUe })).toThrow();
    });
    it('accepts only exact fresh authoritative receiver joins', () => {
        expect(verifiedReceiverMeasurements(snapshot(), now)).toHaveLength(1);
        expect(verifiedReceiverMeasurements(snapshot(), now + 15001)).toEqual([]);
        expect(verifiedReceiverMeasurements(snapshot(), now - 1001)).toEqual([]);
        expect(freshTimestamp(null, now)).toBe(false);
    });
    it.each(['roundId', 'generation', 'policyRevision', 'supi', 'ueIp', 'sessionId', 'podUid', 'tun', 'upf', 'scenario', 'source'])('rejects changed %s', (key) => {
        const s = snapshot();
        Object.assign(s.measurements[0], { [key]: 'other' });
        expect(verifiedReceiverMeasurements(s, now)).toEqual([]);
    });
    it('rejects ambiguous duplicate bindings and measurements', () => {
        const s = snapshot();
        s.perUe.push(s.perUe[0]);
        expect(verifiedReceiverMeasurements(s, now)).toEqual([]);
        const t = snapshot();
        t.measurements.push(t.measurements[0]);
        expect(verifiedReceiverMeasurements(t, now)).toEqual([]);
    });
    it('ping/counts alone, cleaning, and null rates do not animate', () => {
        const s = snapshot();
        s.measurements = [];
        expect(verifiedReceiverMeasurements(s, now)).toEqual([]);
        expect(verifiedReceiverMeasurements({ ...snapshot(), stage: 'cleaning' }, now)).toEqual([]);
        const t = snapshot();
        t.measurements[0].receivedUplinkMbps = null;
        t.measurements[0].receivedDownlinkMbps = null;
        expect(verifiedReceiverMeasurements(t, now)).toEqual([]);
    });
    it('ignores stale polling, old generations, and old policy revisions', () => {
        const s = snapshot();
        expect(acceptScenarioSnapshot(s, { ...s, sequence: 0 })).toBe(s);
        expect(acceptScenarioSnapshot(s, { ...s, sequence: 2, generation: 0 })).toBe(s);
        const t = { ...s, policyRevision: 3 };
        expect(acceptScenarioSnapshot(t, { ...s, sequence: 2 })).toBe(t);
        expect(acceptScenarioSnapshot(s, { ...s, sequence: 2 }).sequence).toBe(2);
    });
    it('rejects malformed observations without fabricating counts or timestamps', () => {
        expect(parseScenarioSnapshot(snapshot())).toEqual(snapshot());
        for (const change of [{ observedAt: undefined }, { sequence: -1 }, { counts: {} }, { measurements: [null] }, { executionOutcome: {} }, { actions: [{ id: 'a', effectVerified: 'yes' }] }]) {
            expect(() => parseScenarioSnapshot({ ...snapshot(), ...change })).toThrow();
        }
    });
});
