import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ActionDetails, ActivitySummary, AgentStatus } from '../../components/OnpremRuntime/NetworkAiPanel';
import { ScenarioAdapterProvider, type ScenarioAdapter } from '../../components/OnpremRuntime/ScenarioContext';
import { startActionEvidenceRead, startActionLogRead } from '../../components/OnpremRuntime/useActionEvidence';
import { actionReadKey, evidenceForRole, parseActionLogs, roundActions, scopedActions, selectedActivityAction, type ActionEvidence, type ActionLogs, type ActionSummary } from '../actionEvidence';
import { readAgentEvents, type AgentEvent, type RoundObservability } from '../roundObservability';
const fixture = vi.hoisted(() => ({ raw: null as ActionLogs | null }));
vi.mock('../../components/OnpremRuntime/useActionEvidence', async (original) => ({
    ...await original<Record<string, unknown>>(),
    useActionEvidence: () => ({ evidence: null, events: [], loading: false, error: '', gap: false, hasMore: false, loadMore: () => { } }),
    useActionLogs: () => ({ lines: fixture.raw?.lines ?? [], sources: fixture.raw?.sources ?? [], status: fixture.raw?.status ?? null,
        reason: fixture.raw?.reason ?? null, loading: false, error: '', gap: false, hasMore: fixture.raw?.hasMore ?? false, loadMore: () => { } }),
}));
const at = '2026-10-18T12:00:00Z';
const action: ActionSummary = { id: 'call:one', decisionId: 'r:1:0', cycle: 0, actionId: 'policy1', callId: 'one', at,
    type: 'tool', action: null, target: 'pod:upf', tool: 'get_nf_resource_usage', scenario: 'video_streaming', role: 'planner', relation: 'call' };
const observation: RoundObservability = { schemaVersion: 'cityverse.round-observability/v1', roundId: 'r', generation: 1, observedAt: at,
    agent: { role: 'planner', callId: 'one' }, decisions: [], changes: [], latestQos: [], actions: [action], capabilities: { podScaling: false, qerPolicy: true } };
const evidence: ActionEvidence = { schemaVersion: 'cityverse.action-evidence/v1', roundId: 'r', generation: 1, cycle: 0, actionId: 'policy1', callId: 'one',
    qer: { status: 'missing', consistency: 'unknown' }, resources: [], diagnostics: [], toolEvidence: [] };
const logs: ActionLogs = { schemaVersion: 'cityverse.action-logs/v1', roundId: 'r', generation: 1, cycle: 0, actionId: 'policy1', callId: 'one',
    role: 'planner', status: 'captured', reason: null, relation: 'action-time', nextCursor: 10, hasMore: false, gap: false,
    sources: [{ sourceRef: 's1', workloadRef: 'pod:upf', podUid: 'uid1', container: 'upf', observedAt: at, evidenceRef: 'saved:1' }],
    lines: [{ sequence: 10, at, text: '<script>redacted diagnostic</script>', sourceRef: 's1' }] };
const reply = (value: unknown) => ({ ok: true, status: 200, json: async () => value });
beforeEach(() => { fixture.raw = null;  });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });
describe('shared Agent and action selection', () => {
    const activityAdapter = (): ScenarioAdapter => ({ snapshot: null, error: '', pending: false, uncertain: false, now: Date.parse(at), fresh: true,
        selectedScenario: 'video_streaming', selectedAgentRole: 'planner', observation: { data: observation, events: [], samples: [], gap: false, truncated: false, error: '' },
    });
    it('filters explicit roles and avoids substituting another action after a scope change', () => {
        const validator: ActionSummary = { ...action, id: 'call:two', callId: 'two', role: 'validator' };
        const rows = roundActions({ ...observation, actions: [action, validator] }, []);
        const filtered = scopedActions(rows, observation, 'video_streaming', null, null, 'validator');
        expect(filtered).toEqual([validator]);
        expect(selectedActivityAction(filtered, action.id, 'two')).toBeNull();
        expect(selectedActivityAction(filtered, null, 'two')).toEqual(validator);
        expect(scopedActions([{ ...action, role: null }], observation, 'video_streaming', null, null, 'planner')).toEqual([]);
    });
    it('isolates keys by owner, generation, cycle, action, call and Agent', () => {
        const keys = [actionReadKey('r', 1, action, null), actionReadKey('r', 1, action, 'planner'), actionReadKey('r', 2, action, 'planner'),
            actionReadKey('other', 1, action, 'planner'), actionReadKey('r', 1, { ...action, cycle: 1 }, 'planner'),
            actionReadKey('r', 1, { ...action, callId: 'two' }, 'planner'), actionReadKey('r', 1, { ...action, actionId: 'policy2' }, 'planner')];
        expect(new Set(keys).size).toBe(keys.length);
    });
    it('keeps a completed-only call selectable when no artifact/start exists in the server summary', () => {
        const retained = { sequence: 99, callId: 'main-validator', cycle: 0, role: 'validator', phase: 'postcheck', tool: 'verify_effect', status: 'completed', at,
            targetRefs: [{ mapNodeId: 'upf', scenario: 'video_streaming', slice: 'embb', workloadRef: 'pod:upf', podUid: 'uid1', shared: false }] } as AgentEvent;
        const rows = roundActions(observation, [retained]);
        const selected = scopedActions(rows, observation, 'video_streaming', null, null, 'validator');
        expect(selected).toHaveLength(1);
        expect(selected[0]).toMatchObject({ id: 'call:main-validator', role: 'validator', callId: 'main-validator', relation: 'call' });
        expect(selected[0].evidenceRef).toBeUndefined();
        expect(selected[0].targetRefs).toEqual(retained.targetRefs);
        expect(roundActions({ ...observation, actions: [...observation.actions!, selected[0]] }, [retained]).filter((a) => a.callId === 'main-validator')).toHaveLength(1);
    });
    it('does not attribute unclassified or other Agent diagnostic data to the selected role', () => {
        const filtered = evidenceForRole({ ...evidence, diagnostics: [
                { role: 'planner', diagnostics: [] }, { role: 'validator', diagnostics: [] }, { diagnostics: [] }
            ],
            toolEvidence: [{ role: 'planner' }, { role: 'executor' }, {}] }, 'planner');
        expect(filtered.diagnostics).toEqual([{ role: 'planner', diagnostics: [] }]);
        expect(filtered.toolEvidence).toEqual([{ role: 'planner' }]);
    });
    it('deduplicates scope labels and distinguishes the role filter from the live executor phase alias', () => {
        const a = activityAdapter();
        a.observation = { ...a.observation!, data: { ...observation, agent: { role: 'executor', phase: 'execute', status: 'started', updatedAt: at } } };
        const refs = [{ mapNodeId: 'upf' as const, scenario: 'video_streaming', slice: 'embb', workloadRef: 'pod:1', podUid: 'uid1', shared: false },
            { mapNodeId: 'upf' as const, scenario: 'video_streaming', slice: 'embb', workloadRef: 'pod:2', podUid: 'uid2', shared: false }];
        const summary = renderToStaticMarkup(<ActivitySummary adapter={a} action={{ ...action, targetRefs: refs }}/>);
        expect(summary.match(/upf \/ embb/g)).toHaveLength(1);
        const agent = renderToStaticMarkup(<AgentStatus adapter={a}/>);
        expect(agent).toContain('data-testid="agent-step-apply" data-active="true" aria-pressed="false"');
        expect(agent).toContain("外框：查看角色 \u00B7 \u25CF：目前執行");
        expect(agent).toContain('executor · execute');
    });
});
describe('server-filtered execution pages', () => {
    it('carries the same role through every page and rejects a foreign role', async () => {
        const fetch = vi.fn((url: string) => Promise.resolve(reply({ events: [{ sequence: url.includes('after=10') ? 20 : 10,
                    cycle: 0, callId: 'one', role: 'planner', status: 'completed' }], nextCursor: url.includes('after=10') ? 20 : 10, hasMore: false, gap: false })));
        vi.stubGlobal('fetch', fetch);
        const signal = new AbortController().signal;
        await readAgentEvents('r', 0, signal, { cycle: 0, role: 'planner' });
        await readAgentEvents('r', 10, signal, { cycle: 0, role: 'planner' });
        expect(fetch.mock.calls.every(([url]) => url.includes('role=planner'))).toBe(true);
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply({ events: [{ sequence: 10, cycle: 0, role: 'validator' }], nextCursor: 10, hasMore: false, gap: false })));
        await expect(readAgentEvents('r', 0, signal, { cycle: 0, role: 'planner' })).rejects.toThrow('Foreign action log');
    });
    it('does not read event logs when only a QER/resource disclosure opens', async () => {
        const receive = vi.fn(), fetch = vi.fn().mockResolvedValue(reply(evidence));
        vi.stubGlobal('fetch', fetch);
        startActionEvidenceRead('r', 1, action, 0, receive, vi.fn(), 'planner', false);
        await vi.waitFor(() => expect(receive).toHaveBeenCalled());
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(fetch.mock.calls[0][0]).toContain('/action-evidence?');
    });
});
describe('saved raw logs', () => {
    it('requires exact owner/action/call/role and a linked Pod source', () => {
        expect(parseActionLogs(logs, 'r', 1, action, 0, 'planner')).toBe(logs);
        for (const changed of [{ roundId: 'foreign' }, { generation: 2 }, { cycle: 1 }, { actionId: 'other' }, { callId: 'other' },
            { role: 'executor' }, { relation: 'current' }, { sources: [] }, { status: 'not_captured' }, { nextCursor: 0 }]) {
            expect(() => parseActionLogs({ ...logs, ...changed }, 'r', 1, action, 0, 'planner')).toThrow();
        }
        expect(() => parseActionLogs({ ...logs, sources: [{ ...logs.sources[0], workloadRef: null, podUid: null }] }, 'r', 1, action, 0, 'planner')).toThrow();
    });
    it('bounds raw text and refuses stuck cursors or invalid source timestamps', () => {
        expect(() => parseActionLogs({ ...logs, lines: [{ ...logs.lines[0], text: 'x'.repeat(4097) }] }, 'r', 1, action, 0, 'planner')).toThrow();
        expect(() => parseActionLogs({ ...logs, hasMore: true }, 'r', 1, action, 10, 'planner')).toThrow();
        expect(() => parseActionLogs({ ...logs, sources: [{ ...logs.sources[0], observedAt: 'invalid' }] }, 'r', 1, action, 0, 'planner')).toThrow();
    });
    it('aborts an obsolete raw read and does not deliver its late payload', async () => {
        let resolve: (v: unknown) => void = () => { };
        let signal: AbortSignal | undefined;
        const receive = vi.fn(), fail = vi.fn();
        vi.stubGlobal('fetch', vi.fn((_url: string, options: {
            signal: AbortSignal;
        }) => {
            signal = options.signal;
            return Promise.resolve({ ok: true, status: 200, json: () => new Promise((done) => { resolve = done; }) });
        }));
        const stop = startActionLogRead('r', 1, action, 0, 'planner', receive, fail);
        await vi.waitFor(() => expect(signal).toBeDefined());
        stop();
        resolve(logs);
        await new Promise((done) => setTimeout(done, 10));
        expect(signal!.aborted).toBe(true);
        expect(receive).not.toHaveBeenCalled();
        expect(fail).not.toHaveBeenCalled();
    });
    it('keeps pagination scoped and surfaces not-captured as an honest result', async () => {
        const receive = vi.fn(), fail = vi.fn();
        const fetch = vi.fn().mockResolvedValue(reply({ ...logs, status: 'not_captured', reason: 'raw_text_not_saved', sources: [], lines: [], nextCursor: 10 }));
        vi.stubGlobal('fetch', fetch);
        startActionLogRead('r', 1, action, 10, 'planner', receive, fail);
        await vi.waitFor(() => expect(receive).toHaveBeenCalled());
        expect(fetch.mock.calls[0][0]).toContain('role=planner');
        expect(fetch.mock.calls[0][0]).toContain('after=10');
        expect(fetch.mock.calls[0][0]).toContain('callId=one');
        expect(receive.mock.calls[0][0].status).toBe('not_captured');
        expect(fail).not.toHaveBeenCalled();
    });
    it('retains a visible action summary and separates collapsed raw text from execution summaries', () => {
        fixture.raw = logs;
        const adapter: ScenarioAdapter = { snapshot: null, error: '', pending: false, uncertain: false, now: Date.parse(at), fresh: true,
            selectedScenario: 'video_streaming', selectedAgentRole: 'planner', observation: { data: observation, events: [], samples: [], gap: false, truncated: false, error: '' },
        };
        const html = renderToStaticMarkup(<ScenarioAdapterProvider value={adapter}><ActionDetails adapter={adapter}/></ScenarioAdapterProvider>);
        expect(html).toContain('data-testid="action-summary"');
        expect(html).toContain('planner · get_nf_resource_usage');
        expect(html).toContain("當時保存的原始 log");
        expect(html).toContain("工具執行摘要與診斷分類");
        expect(html).not.toMatch(/<details[^>]*open/);
        expect(html).toContain('&lt;script&gt;');
        expect(html).not.toContain('<script>');
    });
});

vi.mock('../../../services/browserSession', () => ({ sessionHeaders: () => ({ Authorization: 'Bearer fixture-only', 'X-CityVerse-Session-Id': 'fixture-owner' }) }))
