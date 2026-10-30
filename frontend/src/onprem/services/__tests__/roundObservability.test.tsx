import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { comparableQos, mergeObservationRows, parseObservation, parseQosSample, qosSampleFresh, readAgentEvents, type QosSample, type RoundObservability, type RoundChange } from '../roundObservability';
import { measurementSegments, NetworkAiPanel, selectedRoundChanges, SliceQosSummary } from '../../components/OnpremRuntime/NetworkAiPanel';
import { ScenarioAdapterProvider, type ScenarioAdapter } from '../../components/OnpremRuntime/ScenarioContext';
const now = Date.parse('2026-10-18T08:00:00Z');
const iso = (offset = 0) => new Date(now + offset).toISOString();
function sample(overrides: Partial<QosSample> = {}): QosSample {
    return { id: 'window1:video_streaming', sequence: 1, roundId: 'round1', generation: 1, scenario: 'video_streaming', slice: 'embb',
        stage: 'measuring_baseline', startedAt: iso(-1000), finishedAt: iso(), observedAt: iso(), receivedUplinkMbps: 1,
        receivedDownlinkMbps: 2, rttMs: 45, rttP95Ms: null, lossPercent: null, packets: null, lostPackets: null,
        sampleCount: 1, source: 'receiver', quality: 'observed', comparisonFingerprint: 'load1', policyRevision: 0, ...overrides };
}
function observation(latest = sample()): RoundObservability {
    return { schemaVersion: 'cityverse.round-observability/v1', roundId: 'round1', generation: 1, observedAt: iso(),
        agent: { role: 'validator', phase: 'postcheck', tool: 'verify_effect', status: 'started', cycle: 2, callId: 'call1', startedAt: iso(-1000), updatedAt: iso() },
        decisions: [{ id: 'd1', cycle: 2, action: 'hold', reason: 'No evidence for a write.', outcome: null,
                improvementPercent: null, beforeRef: 'private-ref', afterRef: null, actionRef: null,
                beforeSampleIds: ['window1:video_streaming'], afterSampleIds: [] }], changes: [], latestQos: [latest], capabilities: { podScaling: false, qerPolicy: true } };
}
const adapter = (latest = sample()): ScenarioAdapter => ({ snapshot: null, error: '', pending: false, uncertain: false,
    fresh: true, now, selectedScenario: 'video_streaming',
    observation: { data: observation(latest), events: [], samples: [latest], error: '', gap: false, truncated: false } });
afterEach(() => vi.unstubAllGlobals());
describe('QoS evidence boundaries', () => {
    it('rejects another round or generation and invented p95 from a single RTT', () => {
        expect(() => parseQosSample(sample(), 'round2', 1)).toThrow();
        expect(() => parseQosSample(sample(), 'round1', 2)).toThrow();
        expect(() => parseQosSample(sample({ rttP95Ms: 45 }), 'round1', 1)).toThrow();
        expect(parseQosSample(sample(), 'round1', 1).rttP95Ms).toBeNull();
        expect(() => parseObservation({ ...observation(), latestQos: [sample({ generation: 2 })] }, 'round1', 1)).toThrow();
    });
    it('accepts durable legacy events without optional duration and unknown policy revision', async () => {
        expect(parseQosSample(sample({ policyRevision: null, stage: null }), 'round1', 1).policyRevision).toBeNull();
        expect(parseObservation({ ...observation(), agent: { role: null, phase: null, status: null },
            decisions: [{ ...observation().decisions[0], reason: null, action: null }] }, 'round1', 1).agent?.phase).toBeNull();
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({
                events: [{ sequence: 1, role: 'planner', phase: 'plan', status: 'running' }], nextCursor: 1, gap: false, hasMore: false,
            }) }));
        expect((await readAgentEvents('round1', 0, new AbortController().signal)).events).toHaveLength(1);
    });
    it('keeps measurement age independent from refreshed Agent events', () => {
        const old = sample({ observedAt: iso(-16000) });
        const a = adapter(old);
        expect(qosSampleFresh(old, now)).toBe(false);
        const live = renderToStaticMarkup(<SliceQosSummary adapter={a} title="QoS"/>);
        expect(live).toContain('data-metric-fresh="false"');
        expect(live).not.toContain('45.0 ms');
        const historical = renderToStaticMarkup(<SliceQosSummary adapter={{ ...a, selectedSampleId: old.id }} title="QoS"/>);
        expect(historical).toContain("歷史窗口");
        expect(historical).toContain('45.0 ms');
        expect(historical).toContain(`data-source-observed-at="${old.observedAt}"`);
        expect(historical).not.toContain('RTT p95');
    });
    it('preserves missing values and requires matching observed load for comparison', () => {
        const missing = sample({ quality: 'missing', receivedUplinkMbps: null, receivedDownlinkMbps: null, rttMs: null });
        const html = renderToStaticMarkup(<SliceQosSummary adapter={adapter(missing)} title="QoS"/>);
        expect(html).toContain('qos-metric-receivedUplinkMbps');
        expect(html).toContain('qos-metric-receivedDownlinkMbps');
        expect(html).toContain('data-verdict="unknown"');
        expect(html).not.toContain('0.0 Mbps');
        expect(comparableQos(sample(), sample({ comparisonFingerprint: 'other-load' }))).toBe(false);
        expect(comparableQos(sample(), sample({ comparisonFingerprint: null }))).toBe(false);
        expect(comparableQos(sample(), missing)).toBe(false);
        expect(comparableQos(sample(), sample({ sequence: 2 }))).toBe(true);
    });
    it('never projects another scenario policy or current policy revision into a historical window', () => {
        const a = adapter(sample({ policyRevision: null }));
        a.observation!.data!.changes = [{ id: 'policy1', kind: 'qer_policy', scenario: 'realtime_control', profileId: 'realtime_control-only-profile',
                requested: true, accepted: true, installed: true, effectVerified: false, policyRevision: 20, podChange: 'unknown' }];
        const current = renderToStaticMarkup(<SliceQosSummary adapter={a} title="QoS"/>);
        expect(current).not.toContain('realtime_control-only-profile');
        a.selectedSampleId = 'window1:video_streaming';
        const past = renderToStaticMarkup(<SliceQosSummary adapter={a} title="QoS"/>);
        expect(past).not.toContain('realtime_control-only-profile');
        expect(past).not.toContain('Window policy revision');
        expect(past).not.toContain('UPF Pod');
        expect(past).toContain("歷史窗口");
    });
    it('breaks paths on missing data, time gaps and different loads', () => {
        const a = sample();
        const b = sample({ id: 'b', sequence: 2, startedAt: iso(), finishedAt: iso(1000), observedAt: iso(1000) });
        const gap = sample({ id: 'gap', sequence: 3, startedAt: iso(5000), finishedAt: iso(6000), observedAt: iso(6000) });
        expect(measurementSegments([a, b, gap], 'rttMs').map((s) => s.length)).toEqual([2, 1]);
        expect(measurementSegments([a, { ...b, rttMs: null }, gap], 'rttMs').map((s) => s.length)).toEqual([1, 1]);
        expect(measurementSegments([a, { ...b, comparisonFingerprint: 'different' }], 'rttMs').map((s) => s.length)).toEqual([1, 1]);
        expect(measurementSegments([a, { ...b, startedAt: null }], 'rttMs').map((s) => s.length)).toEqual([1, 1]);
    });
});
describe('event history and UI', () => {
    const change: RoundChange = { id: 'policy1', kind: 'qer_policy', scenario: 'video_streaming', profileId: 'profile-a',
        requested: true, accepted: true, installed: true, effectVerified: false, policyRevision: null, podChange: 'unknown',
        decisionId: 'd1', beforeSampleIds: ['window0:video_streaming'], afterSampleIds: ['window1:video_streaming'], state: 'restored', restorationVerified: true,
        restoredAt: iso(1000), before: { profileId: null, uplinkKbps: { min: 100, max: 100 }, downlinkKbps: { min: 100, max: 200 },
            ueCount: 1, source: 'native-qer-before', state: 'observed' },
        after: { profileId: 'profile-a', uplinkKbps: null, downlinkKbps: null, ueCount: 1,
            source: 'installed-policy-receipt', state: 'installed' } };
    it('shows saved QER before/after evidence and separates later restoration', () => {
        const a = adapter();
        a.observation!.data!.changes = [change];
        a.selectedSampleId = 'window1:video_streaming';
        a.observation!.data!.decisions[0].action = 'apply';
        expect(parseObservation(a.observation!.data, 'round1', 1).changes[0].after?.profileId).toBe('profile-a');
        const html = renderToStaticMarkup(<ScenarioAdapterProvider value={a}><NetworkAiPanel /></ScenarioAdapterProvider>);
        const diff = html.match(/data-testid="qer-before-after"[\s\S]*?<\/div>/)?.[0];
        expect(diff).toContain('UL 100.0 / DL 100.0–200.0 kbps');
        expect(diff).not.toContain('profile-a');
        expect(html).toContain('profile-a');
        expect(html).toContain("安裝憑證");
        expect(html).toContain("清理證據：已還原");
    });
    it('requires exact scenario and selected window or decision for a change', () => {
        const a = adapter();
        a.observation!.data!.changes = [change, { ...change, id: 'realtime_control', scenario: 'realtime_control' }];
        expect(selectedRoundChanges(a).map((c) => c.id)).toEqual(['policy1']);
        expect(selectedRoundChanges({ ...a, selectedSampleId: 'unrelated-window' })).toEqual([]);
        expect(selectedRoundChanges({ ...a, selectedSampleId: 'window1:video_streaming' }).map((c) => c.id)).toEqual(['policy1']);
        expect(selectedRoundChanges({ ...a, selectedDecisionId: 'hold-other-cycle' })).toEqual([]);
    });
    it('never treats a requested profile as an installed after value', () => {
        const a = adapter();
        a.observation!.data!.changes = [{ ...change, installed: false, after: null, restorationVerified: null }];
        a.observation!.data!.decisions[0].action = 'apply';
        const html = renderToStaticMarkup(<ScenarioAdapterProvider value={a}><NetworkAiPanel /></ScenarioAdapterProvider>);
        const diff = html.match(/data-testid="qer-before-after"[\s\S]*?<\/div>/)?.[0];
        expect(diff).not.toContain('profile-a');
        expect(diff).not.toContain("安裝憑證");
        expect(parseObservation(a.observation!.data, 'round1', 1).changes[0].after).toBeNull();
        expect(() => parseObservation({ ...a.observation!.data, changes: [{ ...change, installed: false }] }, 'round1', 1)).toThrow();
        expect(() => parseObservation({ ...a.observation!.data, changes: [{ ...change, before: {
                        ...change.before, uplinkKbps: { min: 200, max: 100 },
                    } }] }, 'round1', 1)).toThrow();
    });
    it('deduplicates overlapping pages, preserves order and bounds memory', () => {
        expect(mergeObservationRows([{ sequence: 2, status: 'started' }, { sequence: 1, status: 'completed' }], [{ sequence: 2, status: 'completed' }, { sequence: 3, status: 'started' }], 2))
            .toEqual([{ sequence: 2, status: 'completed' }, { sequence: 3, status: 'started' }]);
    });
    it('rejects regressing cursors instead of silently losing events', async () => {
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ events: [], nextCursor: 1, gap: false, hasMore: false }) }));
        await expect(readAgentEvents('round1', 2, new AbortController().signal)).rejects.toThrow('cursor');
    });
    it('shows role/tool/hold and keeps logs and inline daily observation collapsed', () => {
        const html = renderToStaticMarkup(<ScenarioAdapterProvider value={adapter()}><NetworkAiPanel /></ScenarioAdapterProvider>);
        expect(html).toContain('validator');
        expect(html).toContain('verify_effect');
        expect(html).toContain("維持現況 \u00B7 無寫入");
        expect(html).not.toContain('action-resource-details');
        expect(html).toContain('action-log-details');
        expect(html).not.toMatch(/<details[^>]*open/);
        expect(html).not.toContain('fixed bottom-4 right-4');
    });
    it('shows an empty history instead of fabricating a trend', () => {
        const a = adapter();
        a.observation!.samples = [];
        const html = renderToStaticMarkup(<ScenarioAdapterProvider value={a}><NetworkAiPanel outcomes/></ScenarioAdapterProvider>);
        expect(html).toContain('qos-timeline-empty');
        expect(html).not.toContain('<polyline');
    });
});

vi.mock('../../../services/browserSession', () => ({ sessionHeaders: () => ({ Authorization: 'Bearer fixture-only', 'X-CityVerse-Session-Id': 'fixture-owner' }) }))
