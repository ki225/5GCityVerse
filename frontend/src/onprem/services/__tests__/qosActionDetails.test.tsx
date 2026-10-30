import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { actionRelatedPanels, parseActionEvidence, parseActionSummary, scopedActions, type ActionEvidence, type ActionSummary, type ActionResource } from '../actionEvidence';
import { parseQosSample, qosMetricVerdict, type QosSample } from '../roundObservability';
import { ActionDetails, ResourceEvidence, SliceQosSummary } from '../../components/OnpremRuntime/NetworkAiPanel';
import { ScenarioAdapterProvider, type ScenarioAdapter } from '../../components/OnpremRuntime/ScenarioContext';
import { startActionEvidenceRead } from '../../components/OnpremRuntime/useActionEvidence';
const detailFixture = vi.hoisted(() => ({ evidence: null as ActionEvidence | null }));
vi.mock('../../components/OnpremRuntime/useActionEvidence', async (original) => ({
    ...await original<Record<string, unknown>>(),
    useActionEvidence: () => ({ evidence: detailFixture.evidence, events: [], loading: false, error: '', gap: false, hasMore: false, loadMore: () => { } }),
}));
const now = Date.parse('2026-10-18T12:00:00Z'), at = new Date(now).toISOString();
const sample: QosSample = { id: 'w1:video_streaming', sequence: 1, roundId: 'r1', generation: 1, scenario: 'video_streaming', slice: 'embb',
    stage: 'measuring_result', startedAt: new Date(now - 5000).toISOString(), finishedAt: at, observedAt: at,
    receivedUplinkMbps: 1.8, receivedDownlinkMbps: 1.8, rttMs: 45, rttP95Ms: null, lossPercent: 0, packets: 100, lostPackets: 0,
    sampleCount: 2, source: 'receiver', quality: 'observed', comparisonFingerprint: 'load1', policyRevision: 1,
    targets: { revision: 'v1', source: 'operator-experiment', scenario: 'video_streaming', slice: 'embb', scope: 'scenario-total',
        throughputStatistic: 'sum', rttStatistic: 'mean', lossStatistic: 'packet-ratio', minUplinkMbps: 1.6, minDownlinkMbps: 1.6, maxRttMs: 50, maxLossPercent: 1 },
    assessment: { status: 'met', reason: 'criteria_evaluated', metrics: { receivedUplinkMbps: 'met', receivedDownlinkMbps: 'met', rttMs: 'met', lossPercent: 'met' } },
    rtt: { source: 'native-ue-tun-icmp', status: 'observed', statistic: 'mean', sampleCount: 10, transmitted: 10, received: 10, startedAt: at, finishedAt: at, reason: null, validUeCount: 2, expectedUeCount: 2, sameTrafficWindow: false, relation: 'adjacent-probe-window' } };
const action: ActionSummary = { id: 'r1:1:0', decisionId: 'r1:1:0', cycle: 0, actionId: 'policy1', callId: null, at,
    type: 'decision', action: 'apply', target: 'profile1', scenario: 'video_streaming', relation: 'action' };
const evidence: ActionEvidence = { schemaVersion: 'cityverse.action-evidence/v1', roundId: 'r1', generation: 1, cycle: 0, actionId: 'policy1', callId: null,
    qer: { status: 'partial', consistency: 'unknown', reason: 'readback_missing' }, resources: [], diagnostics: [], toolEvidence: [] };
const adapter = (): ScenarioAdapter => ({ snapshot: null, error: '', fresh: true, now, pending: false, uncertain: false,
    selectedScenario: 'video_streaming', observation: { data: {
            schemaVersion: 'cityverse.round-observability/v1', roundId: 'r1', generation: 1, observedAt: at, agent: null,
            decisions: [], changes: [], actions: [action], latestQos: [sample], capabilities: { podScaling: false, qerPolicy: true }
        },
        samples: [sample], events: [], gap: false, truncated: false, error: '' } });
beforeEach(() => { detailFixture.evidence = null;  });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });
describe('objective validity and scope', () => {
    it('shows scenario-total received values, frozen objectives and independent probe semantics', () => {
        expect(parseQosSample(sample, 'r1', 1)).toBe(sample);
        const html = renderToStaticMarkup(<SliceQosSummary adapter={adapter()} title="QoS"/>);
        expect(html).toContain("範圍：所選情境總量");
        expect(html).toContain('有效 UE: 2');
        expect(html).toContain('≥ 1.6 Mbps');
        expect(html).toContain("平均 RTT");
        expect(html).toContain('獨立探測（相鄰窗口）');
        expect(html).not.toContain('RTT p95');
        expect(html).not.toContain('UPF Pod');
        expect(qosMetricVerdict(sample, 'receivedUplinkMbps', now)).toBe('met');
        expect(qosMetricVerdict(sample, 'rttMs', now)).toBe('unknown');
    });
    it('never passes stale, missing, contradictory or unavailable-source metrics', () => {
        expect(qosMetricVerdict(sample, 'receivedUplinkMbps', now + 16000)).toBe('unknown');
        expect(qosMetricVerdict({ ...sample, receivedUplinkMbps: null }, 'receivedUplinkMbps', now)).toBe('unknown');
        expect(qosMetricVerdict({ ...sample, receivedUplinkMbps: 0.5 }, 'receivedUplinkMbps', now)).toBe('unknown');
        expect(qosMetricVerdict({ ...sample, quality: 'missing' }, 'lossPercent', now)).toBe('unknown');
        const a = adapter();
        a.observation!.error = 'offline';
        const html = renderToStaticMarkup(<SliceQosSummary adapter={a} title="QoS"/>);
        expect(html).not.toContain('data-verdict="met"');
    });
    it('rejects foreign slice, unsupported per-UE targets and statistic/unit ambiguity', () => {
        expect(() => parseQosSample({ ...sample, targets: { ...sample.targets, slice: 'urllc' } }, 'r1', 1)).toThrow();
        expect(() => parseQosSample({ ...sample, targets: { ...sample.targets, scope: 'per-ue' } }, 'r1', 1)).toThrow();
        expect(() => parseQosSample({ ...sample, targets: { ...sample.targets, throughputStatistic: 'mean' } }, 'r1', 1)).toThrow();
        expect(() => parseQosSample({ ...sample, targets: { ...sample.targets, maxLossPercent: 101 } }, 'r1', 1)).toThrow();
    });
    it('keeps historical verdict and objective revision without borrowing current targets', () => {
        expect(qosMetricVerdict(sample, 'receivedDownlinkMbps', now + 16000, true)).toBe('met');
        const a = adapter();
        a.now += 16000;
        a.selectedSampleId = sample.id;
        a.observation!.data!.latestQos = [{ ...sample, targets: { ...sample.targets!, revision: 'v2', minUplinkMbps: 99 } }];
        const html = renderToStaticMarkup(<SliceQosSummary adapter={a} title="QoS"/>);
        expect(html).toContain('v1');
        expect(html).not.toContain('99.0');
    });
});
describe('action detail identity and relevance', () => {
    it('keeps optional old API fields compatible while exposing only related closed disclosures', () => {
        const a = adapter();
        const html = renderToStaticMarkup(<ScenarioAdapterProvider value={a}><ActionDetails adapter={a}/></ScenarioAdapterProvider>);
        expect(html).toContain('action-qer-details');
        expect(html).toContain('action-log-details');
        expect(html).not.toContain('action-resource-details');
        expect(html).not.toMatch(/<details[^>]*open/);
        expect(actionRelatedPanels({ ...action, action: 'hold', target: 'profile' })).toEqual({ qer: false, resources: false, logs: true });
        expect(actionRelatedPanels({ ...action, action: null, tool: 'get_nf_resource_usage' }).resources).toBe(true);
        expect(actionRelatedPanels({ ...action, action: null, target: 'unknown' })).toEqual({ qer: false, resources: false, logs: true });
    });
    it('validates exact round/generation/action/call and preserves partial readback', () => {
        expect(parseActionEvidence(evidence, 'r1', 1, action).qer.consistency).toBe('unknown');
        expect(() => parseActionEvidence(evidence, 'r2', 1, action)).toThrow();
        expect(() => parseActionEvidence(evidence, 'r1', 2, action)).toThrow();
        expect(() => parseActionEvidence(evidence, 'r1', 1, { ...action, actionId: 'other' })).toThrow();
        expect(() => parseActionEvidence(evidence, 'r1', 1, { ...action, callId: 'foreign' })).toThrow();
        expect(() => parseActionSummary({ ...action, cycle: -1 })).toThrow();
        expect(scopedActions([action], undefined, 'realtime_control')).toEqual([]);
    });
    it('renders saved native readback and restoration separately from requested rates', () => {
        const a = adapter();
        a.observation!.data!.changes = [{ id: 'policy1', decisionId: action.id, cycle: 0, scenario: 'video_streaming', profileId: 'profile1', kind: 'qer_policy',
                requested: true, accepted: true, installed: true, effectVerified: false, policyRevision: 1, podChange: 'unknown', restorationVerified: true, restoredAt: at }];
        detailFixture.evidence = { ...evidence, qer: { status: 'observed', consistency: 'consistent', observedAt: at,
                requested: { profileId: 'profile1', uplinkKbps: 200, downlinkKbps: 300 },
                before: { ueCount: 1, uplinkKbps: { min: 100, max: 100 }, downlinkKbps: { min: 100, max: 100 } },
                readback: { ueCount: 1, uplinkKbps: { min: 200, max: 200 }, downlinkKbps: { min: 300, max: 300 } } } };
        const html = renderToStaticMarkup(<ScenarioAdapterProvider value={a}><ActionDetails adapter={a}/></ScenarioAdapterProvider>);
        expect(html).toContain('consistent');
        expect(html).toContain('UL 200.0 / DL 300.0 kbps');
        expect(html).toContain("原生 QER readback");
        expect(html).toContain("清理證據：已還原");
        expect(html).not.toContain('action-resource-details');
        expect(html).not.toMatch(/<details[^>]*open/);
    });
    it('renders NF-scoped resources without treating shared usage as a slice allocation or desired replica count', () => {
        const resource: ActionResource = { status: 'available', observedAt: at, workloads: [{ workloadRef: 'pod:smf', podUid: 'uid1', nfType: 'smf',
                    sharing: 'shared', ready: true, readyPods: 1, desiredPods: null, restarts: 0, phase: 'Running' }], metrics: [{ workloadRef: 'pod:smf', podUid: 'uid1',
                    nfType: 'smf', sharing: 'shared', observedAt: at, identityStatus: 'uid_matched', status: 'observed', containers: [{ name: 'smf', cpuCores: 0.015, memoryBytes: 1048576 }] }] };
        const html = renderToStaticMarkup(<ResourceEvidence resources={[resource]} adapter={adapter()}/>);
        expect(html).toContain("共享 NF／Pod");
        expect(html).toContain('15.0 mCPU');
        expect(html).toContain('1.0 MiB');
        expect(html).toContain('期望副本');
        expect(html).not.toContain('100%');
        const stale = renderToStaticMarkup(<ResourceEvidence resources={[resource]} adapter={{ ...adapter(), now: now + 16000 }}/>);
        expect(stale).not.toContain('15.0 mCPU');
        expect(stale).toContain("資源量測已過期");
        resource.metrics[0].identityStatus = 'unverified';
        expect(renderToStaticMarkup(<ResourceEvidence resources={[resource]} adapter={adapter()}/>)).not.toContain('15.0 mCPU');
    });
});
describe('bounded action read lifetime', () => {
    it('cancels an obsolete detail read and never delivers its late result', async () => {
        let resolveEvidence: ((value: unknown) => void) | undefined;
        const receive = vi.fn(), failure = vi.fn();
        vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve({ ok: true, status: 200, json: () => url.includes('/action-evidence')
                ? new Promise((resolve) => { resolveEvidence = resolve; }) : Promise.resolve({ events: [], nextCursor: 0, hasMore: false, gap: false }) })));
        const stop = startActionEvidenceRead('r1', 1, action, 0, receive, failure);
        await vi.waitFor(() => expect(resolveEvidence).toBeDefined());
        stop();
        resolveEvidence!(evidence);
        await new Promise((resolve) => setTimeout(resolve, 10));
        expect(receive).not.toHaveBeenCalled();
        expect(failure).not.toHaveBeenCalled();
    });
    it('validates paginated action logs and isolates unavailable detail reads', async () => {
        const receive = vi.fn(), failure = vi.fn();
        vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve({ ok: true, status: 200, json: async () => url.includes('/action-evidence') ? evidence
                : { events: [{ sequence: 2, cycle: 0, status: 'completed', durationMs: 1 }], nextCursor: 2, hasMore: true, gap: true } })));
        startActionEvidenceRead('r1', 1, action, 1, receive, failure);
        await vi.waitFor(() => expect(receive).toHaveBeenCalled());
        expect(receive.mock.calls[0].slice(2)).toEqual([2, true, true]);
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 503, text: async () => '{}' }));
        startActionEvidenceRead('r1', 1, action, 0, receive, failure);
        await vi.waitFor(() => expect(failure).toHaveBeenCalled());
        expect(adapter().observation!.data!.latestQos[0]).toBe(sample);
    });
});

vi.mock('../../../services/browserSession', () => ({ sessionHeaders: () => ({ Authorization: 'Bearer fixture-only', 'X-CityVerse-Session-Id': 'fixture-owner' }) }))
