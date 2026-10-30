const formalTest = vi.hoisted(() => ({ workspace: { intentId: '', serial: 1 } }));
vi.mock('./city-map/context/FormalIntentContext', () => ({ useFormalIntent: () => formalTest }));
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import type { ServiceIntentView } from './services/intents';
const h = vi.hoisted(() => ({ values: [] as any[], cursor: 0, dirty: false, effects: [] as any[] }));
vi.mock('react', async (importOriginal) => {
    const real = await importOriginal<typeof import('react')>();
    return { ...real,
        useState(initial: any) { const i = h.cursor++; if (!(i in h.values))
            h.values[i] = typeof initial === 'function' ? initial() : initial; return [h.values[i], (v: any) => { h.values[i] = typeof v === 'function' ? v(h.values[i]) : v; h.dirty = true; }]; },
        useRef(initial: any) { const i = h.cursor++; if (!(i in h.values))
            h.values[i] = { current: initial }; return h.values[i]; },
        useEffect(fn: any, deps: any[]) { const i = h.cursor++; const old = h.values[i]; if (!old || deps.some((v, j) => !Object.is(v, old.deps[j]))) {
            h.effects.push(() => { old?.cleanup?.(); h.values[i] = { deps, cleanup: fn() }; });
        } },
    };
});
vi.mock('./services/intents', async (importOriginal) => ({ ...await importOriginal<typeof import('./services/intents')>(),
    listServiceIntents: vi.fn(), getServiceIntent: vi.fn(), getIntentMembers: vi.fn(), setIntentControlToken: vi.fn(),
    submitServiceIntent: vi.fn(), releaseServiceIntent: vi.fn(), reconcileServiceIntent: vi.fn(),
}));
import { IntentWorkspace } from './onprem/components/IntentWorkspace/IntentWorkspace';
import { intentEvidence } from './onprem/components/IntentWorkspace/intentPresentation';
import * as api from './services/intents';
function proof(): ServiceIntentView {
    const now = Date.now() / 1000;
    return { intent_id: 'i', revision: 1, status: 'satisfied', provenance: 'user', selector: { service_types: ['video'] }, valid_from: now - 10, valid_until: now + 600,
        objectives: [{ metric: 'dl_throughput', target: 8 }], membership: { members: [{ session_token: 's' }], pending_count: 0 },
        latest_operation: { intent_id: 'i', operation_id: 'op', status: 'completed', plan: { plan_id: 'p', intent_id: 'i', intent_revision: 1, actions: [] },
            after: { observation_id: 'a', observed_at: now, maximum_age_seconds: 30 },
            effect: { met: true, protected_services_met: true, same_membership: true, fresh_window: true, owner_id: 'i', operation_id: 'op', source: 'current-native-receiver', observation_id: 'a', observed_at: now, objectives: [{ status: 'passed' }] } } };
}
function nodes(value: any): any[] { return !value || typeof value !== 'object' ? [] : [value, ...[value.props?.children].flat(Infinity).flatMap(nodes)]; }
function host() {
    let tree: any, html = '';
    function render() { h.cursor = 0; h.dirty = false; tree = IntentWorkspace(); html = renderToStaticMarkup(tree); while (h.effects.length)
        h.effects.shift()(); }
    async function settle() { for (let i = 0; i < 20; i++) {
        await Promise.resolve();
        if (h.dirty)
            render();
    } }
    render();
    return { render, settle, get tree() { return tree; }, get html() { return html; } };
}
beforeEach(() => { h.values = []; h.effects = []; h.cursor = 0; h.dirty = false; vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-28T04:00:00Z')); vi.clearAllMocks(); });
afterEach(() => { for (const v of h.values)
    v?.cleanup?.(); vi.useRealTimers(); });
it('opening and reading the real workspace dispatches no mutation', async () => {
    vi.mocked(api.listServiceIntents).mockResolvedValue({ items: [] });
    const ui = host();
    ui.render();
    await ui.settle();
    expect(api.listServiceIntents).toHaveBeenCalledTimes(1);
    expect(api.submitServiceIntent).not.toHaveBeenCalled();
    expect(api.releaseServiceIntent).not.toHaveBeenCalled();
    expect(api.reconcileServiceIntent).not.toHaveBeenCalled();
});
it('an unresolved refresh cannot indefinitely preserve a stale confirmed service milestone', async () => {
    const view = proof();
    vi.mocked(api.listServiceIntents).mockResolvedValue({ items: [{ ...view, source_event_id: 'TARGET_EVENT' }] });
    vi.mocked(api.getServiceIntent).mockResolvedValue(view);
    vi.mocked(api.getIntentMembers).mockResolvedValue({ items: [{ session_token: 's' }], total: 1, rejected: [] });
    const ui = host();
    ui.render();
    await ui.settle();
    nodes(ui.tree).find(n => n.type === 'button' && n.props.children?.[0]?.props?.children === 'TARGET_EVENT').props.onClick();
    ui.render();
    await ui.settle();
    expect(ui.html).toContain('data-evidence-stage="satisfied" data-evidence-state="confirmed"');
    vi.mocked(api.listServiceIntents).mockImplementation(() => new Promise(() => { }));
    await vi.advanceTimersByTimeAsync(31000);
    await ui.settle();
    expect(intentEvidence(view).satisfied).toBe(false);
    expect(ui.html).not.toContain('data-evidence-stage="satisfied" data-evidence-state="confirmed"');
});
it.each(['cancelled', 'notaccepted', 'unknown'])('does not assert current installed milestone for %s operation', status => {
    const view = proof();
    view.latest_operation!.status = status;
    view.latest_operation!.plan!.actions = [{ action_id: 'a', kind: 'update_qos' }];
    view.latest_operation!.receipts = [{ action_id: 'a', state: 'installation_confirmed', installed: { version: 'native' }, installation_confirmed_at: Date.now() / 1000 - 1 }];
    expect(intentEvidence(view).milestones[2].state).not.toBe('confirmed');
});
