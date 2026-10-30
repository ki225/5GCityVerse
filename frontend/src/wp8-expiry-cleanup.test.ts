const formalTest = vi.hoisted(() => ({ workspace: { intentId: '', serial: 1 } }));
vi.mock('./city-map/context/FormalIntentContext', () => ({ useFormalIntent: () => formalTest }));
// Hook/transport harness adapted from preserved independent P1 reproduction; no real backend success or I/O.
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
import { intentEvidence, nextEvidenceDeadline } from './onprem/components/IntentWorkspace/intentPresentation';
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
    v?.cleanup?.(); vi.unstubAllGlobals(); vi.useRealTimers(); });
async function displayed() {
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
    return { ui, view };
}
it('one original-deadline timer expires service display without adding network polling', async () => {
    const { ui } = await displayed();
    vi.mocked(api.listServiceIntents).mockImplementation(() => new Promise(() => { }));
    await vi.advanceTimersByTimeAsync(5000);
    await ui.settle();
    const calls = vi.mocked(api.listServiceIntents).mock.calls.length;
    expect(vi.getTimerCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(26000);
    await ui.settle();
    expect(ui.html).toContain("目前達標證據未驗證");
    expect(ui.html).not.toContain('data-evidence-stage="satisfied" data-evidence-state="confirmed"');
    expect(api.listServiceIntents).toHaveBeenCalledTimes(calls);
    expect(vi.getTimerCount()).toBe(1); // Remaining contract expiry only, not periodic proof polling.
});
it('visibility return invalidates the original expired window even when browser timers were throttled', async () => {
    const listeners = new Set<() => void>();
    const doc = { visibilityState: 'visible', addEventListener: vi.fn((name: string, fn: () => void) => { if (name === 'visibilitychange')
            listeners.add(fn); }), removeEventListener: vi.fn((name: string, fn: () => void) => { if (name === 'visibilitychange')
            listeners.delete(fn); }) };
    vi.stubGlobal('document', doc);
    const { ui } = await displayed();
    expect(listeners.size).toBe(1);
    vi.setSystemTime(Date.now() + 31000); // No timers run: explicit hidden-tab throttle/wall advance.
    doc.visibilityState = 'hidden';
    for (const fn of listeners)
        fn();
    await ui.settle();
    expect(ui.html).toContain('data-evidence-stage="satisfied" data-evidence-state="confirmed"');
    doc.visibilityState = 'visible';
    for (const fn of listeners)
        fn();
    await ui.settle();
    expect(ui.html).not.toContain('data-evidence-stage="satisfied" data-evidence-state="confirmed"');
    expect(ui.html).toContain("目前達標證據未驗證");
    expect(listeners.size).toBe(1);
    nodes(ui.tree).find(n => n.type === 'button' && n.props['aria-label'] === "關閉服務意圖").props.onClick();
    ui.render();
    await ui.settle();
    expect(listeners.size).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
});
it('unmount cancels proof/refresh timers and visibility subscriptions without mutating native state', async () => {
    const listeners = new Set<() => void>();
    vi.stubGlobal('document', { visibilityState: 'visible', addEventListener: (_n: string, f: () => void) => listeners.add(f), removeEventListener: (_n: string, f: () => void) => listeners.delete(f) });
    await displayed();
    expect(vi.getTimerCount()).toBeLessThanOrEqual(2);
    expect(listeners.size).toBe(1);
    for (const value of h.values)
        value?.cleanup?.();
    expect(vi.getTimerCount()).toBe(0);
    expect(listeners.size).toBe(0);
    await vi.advanceTimersByTimeAsync(60000);
    expect(api.submitServiceIntent).not.toHaveBeenCalled();
    expect(api.reconcileServiceIntent).not.toHaveBeenCalled();
    expect(api.releaseServiceIntent).not.toHaveBeenCalled();
});
it('uses original boundaries and stops scheduling once all evidence boundaries have passed', () => {
    const view = proof(), now = Date.now() / 1000;
    expect(nextEvidenceDeadline(view, now)).toBe(now + 30);
    expect(nextEvidenceDeadline(view, now + 31)).toBe(now + 600);
    expect(nextEvidenceDeadline(view, now + 601)).toBeNull();
    expect(nextEvidenceDeadline(null, now)).toBeNull();
    expect(view.latest_operation!.after!.observed_at).toBe(now);
});
it.each(['cancelled', 'notaccepted', 'unknown'])('keeps prior installation history without asserting %s installed the current plan or restored it', status => {
    const view = proof();
    view.latest_operation!.status = status;
    view.latest_operation!.plan!.actions = [{ action_id: 'a', kind: 'update_qos', target_id: 'session' }];
    view.latest_operation!.receipts = [{ action_id: 'a', state: 'installation_confirmed', installed: { version: 'native' }, installation_confirmed_at: Date.now() / 1000 - 1 }];
    const step = intentEvidence(view).milestones[2];
    expect(step.state).toBe('historical');
    expect(step.reason).toBe('prior_installation_current_operation_unverified');
    expect(view.latest_operation!.receipts[0]).toHaveProperty('installed.version', 'native');
});
it('current installation requires current operation/target/action receipt identity, not a reused action_id', () => {
    const view = proof();
    view.latest_operation!.plan!.actions = [{ action_id: 'a', kind: 'update_qos', target_id: 'session' }];
    const receipt = { action_id: 'a', state: 'installation_confirmed', installed: { version: 'native' }, request: { action: 'update_qos', target_id: 'session', operation_id: 'op' } };
    view.latest_operation!.receipts = [receipt];
    expect(intentEvidence(view).milestones[2].state).toBe('confirmed');
    for (const request of [{ ...receipt.request, operation_id: 'old' }, { ...receipt.request, target_id: 'foreign' }, { ...receipt.request, action: 'resize_nf' }]) {
        view.latest_operation!.receipts = [{ ...receipt, request }];
        expect(intentEvidence(view).milestones[2].state).toBe('historical');
    }
});
