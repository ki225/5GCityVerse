const formalTest = vi.hoisted(() => ({ workspace: { intentId: '', serial: 1 } }));
vi.mock('./city-map/context/FormalIntentContext', () => ({ useFormalIntent: () => formalTest }));
// Explicit existing hook/GET doubles; no actual backend/native I/O.
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
    getIntentOperationDetail: vi.fn(), listServiceIntents: vi.fn(), getServiceIntent: vi.fn(), getIntentMembers: vi.fn(), setIntentControlToken: vi.fn(),
    submitServiceIntent: vi.fn(), releaseServiceIntent: vi.fn(), reconcileServiceIntent: vi.fn(),
}));
import { IntentWorkspace } from './onprem/components/IntentWorkspace/IntentWorkspace';
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
const OP = '82ebb343-2690-43ee-a740-6607d1efaa2a';
const SECOND = '00000000-0000-4000-8000-000000000001';
const detail = (id = OP, intent = 'i') => ({ operation_id: id, intent_id: intent, status: 'needs_definition', kind: 'reconcile', stage: 'needs_definition', error_code: 'model_proposal_unavailable', category: 'model', readback_reason: 'provider_unavailable' });
async function displayed() {
    const view = proof();
    view.status = 'expired';
    vi.mocked(api.listServiceIntents).mockResolvedValue({ items: [{ ...view, source_event_id: 'TARGET_EVENT' }, { ...view, intent_id: 'j', source_event_id: 'OTHER_EVENT' }] });
    vi.mocked(api.getServiceIntent).mockImplementation(async (id) => ({ ...view, intent_id: id }));
    vi.mocked(api.getIntentMembers).mockResolvedValue({ items: [{ session_token: 's' }], total: 1, rejected: [] });
    const ui = host();
    ui.render();
    await ui.settle();
    nodes(ui.tree).find(n => n.type === 'button' && n.props.children?.[0]?.props?.children === 'TARGET_EVENT').props.onClick();
    ui.render();
    await ui.settle();
    return ui;
}
function submit(ui: ReturnType<typeof host>, id = OP) {
    nodes(ui.tree).find(n => n.type === 'input' && n.props.id === 'intent-operation-id').props.onChange({ target: { value: id } });
    ui.render();
    const section = nodes(ui.tree).find(n => n.type === 'section' && n.props['aria-label'] === "精確操作唯讀查詢");
    nodes(section).find(n => n.type === 'form').props.onSubmit({ preventDefault() { } });
    ui.render();
}
const stages = (html: string) => html.match(/data-evidence-stage="[^"]+" data-evidence-state="[^"]+"/g);
it('manual exact lookup exposes original failure while current milestones stay untouched and no writes occur', async () => {
    const ui = await displayed();
    const before = stages(ui.html);
    expect(api.getIntentOperationDetail).not.toHaveBeenCalled();
    vi.mocked(api.getIntentOperationDetail).mockResolvedValue(detail());
    submit(ui);
    await ui.settle();
    expect(api.getIntentOperationDetail).toHaveBeenCalledWith(OP, 'i', expect.any(AbortSignal));
    expect(ui.html).toContain('model_proposal_unavailable');
    expect(ui.html).toContain("此為單次操作的獨立紀錄");
    expect(stages(ui.html)).toEqual(before);
    expect(api.submitServiceIntent).not.toHaveBeenCalled();
    expect(api.releaseServiceIntent).not.toHaveBeenCalled();
    expect(api.reconcileServiceIntent).not.toHaveBeenCalled();
});
it('never displays a returned foreign intent operation', async () => {
    const ui = await displayed();
    vi.mocked(api.getIntentOperationDetail).mockResolvedValue(detail(OP, 'foreign'));
    submit(ui);
    await ui.settle();
    expect(ui.html).toContain('operation_not_in_selected_intent');
    expect(ui.html).not.toContain('model_proposal_unavailable');
});
it('replacing a query aborts and ignores its late response', async () => {
    const ui = await displayed();
    let late!: (v: ReturnType<typeof detail>) => void;
    vi.mocked(api.getIntentOperationDetail).mockImplementationOnce(() => new Promise(r => { late = r; })).mockResolvedValueOnce({ ...detail(SECOND), error_code: 'second_failure' });
    submit(ui);
    submit(ui, SECOND);
    await ui.settle();
    const first = vi.mocked(api.getIntentOperationDetail).mock.calls[0][2]!;
    expect(first.aborted).toBe(true);
    late(detail());
    await ui.settle();
    expect(ui.html).toContain('second_failure');
    expect(ui.html).not.toContain('model_proposal_unavailable');
});
it('switching selected intent rejects late original operation response and resets query fields', async () => {
    const ui = await displayed();
    let late!: (v: ReturnType<typeof detail>) => void;
    vi.mocked(api.getIntentOperationDetail).mockImplementationOnce(() => new Promise(r => { late = r; }));
    submit(ui);
    nodes(ui.tree).find(n => n.type === 'button' && n.props.children?.[0]?.props?.children === 'OTHER_EVENT').props.onClick();
    ui.render();
    await ui.settle();
    late(detail());
    await ui.settle();
    expect(ui.html).not.toContain('model_proposal_unavailable');
    expect(nodes(ui.tree).find(n => n.type === 'input' && n.props.id === 'intent-operation-id').props.value).toBe('');
});
it('failed retry resets previous detail and never renders raw failure text', async () => {
    const ui = await displayed();
    vi.mocked(api.getIntentOperationDetail).mockResolvedValueOnce(detail()).mockRejectedValueOnce(new Error('private-secret-body must not display'));
    submit(ui);
    await ui.settle();
    expect(ui.html).toContain('model_proposal_unavailable');
    submit(ui, SECOND);
    await ui.settle();
    expect(ui.html).not.toContain('model_proposal_unavailable');
    expect(ui.html).not.toContain('private-secret-body');
    expect(ui.html).toContain('operation_response_unavailable');
});
it('close and unmount abort a pending exact query and cannot resurrect details from late results', async () => {
    const ui = await displayed();
    let late!: (v: ReturnType<typeof detail>) => void;
    vi.mocked(api.getIntentOperationDetail).mockImplementationOnce(() => new Promise(r => { late = r; }));
    submit(ui);
    nodes(ui.tree).find(n => n.type === 'button' && n.props['aria-label'] === "關閉服務意圖").props.onClick();
    ui.render();
    await ui.settle();
    expect(vi.mocked(api.getIntentOperationDetail).mock.calls[0][2]!.aborted).toBe(true);
    late(detail());
    await ui.settle();
    expect(ui.html).not.toContain('model_proposal_unavailable');
    for (const v of h.values)
        v?.cleanup?.();
    h.dirty = false;
    expect(vi.getTimerCount()).toBe(0);
});
