import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const hooks = vi.hoisted(() => ({
    effects: [] as Array<() => void | (() => void)>,
    setters: [] as Array<ReturnType<typeof vi.fn>>,
}));
// Execute the real effect in the existing Node test environment. Only React's
// host hooks are replaced; the API client and deferred HTTP promises are real.
vi.mock('react', async () => ({
    ...await vi.importActual<typeof import('react')>('react'),
    useEffect: (effect: () => void | (() => void)) => { hooks.effects.push(effect); },
    useState: (initial: unknown) => {
        const setter = vi.fn();
        hooks.setters.push(setter);
        return [initial, setter];
    },
}));
vi.mock('../../../../store/appStore', () => ({ useAppStore: () => vi.fn() }));
import { DailyNativeObservationPanel } from '../DailyNativeObservationPanel';
function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((done) => { resolve = done; });
    return { promise, resolve };
}
function response(status: number, body: unknown): Response {
    return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}
function mount() {
    DailyNativeObservationPanel({});
    const cleanup = hooks.effects[hooks.effects.length - 1]();
    if (typeof cleanup !== 'function')
        throw new Error('Panel did not install effect cleanup');
    return cleanup;
}
beforeEach(() => {
    hooks.effects.length = 0;
    hooks.setters.length = 0;
    vi.useFakeTimers();
    vi.stubGlobal('window', { setInterval: globalThis.setInterval, clearInterval: globalThis.clearInterval,
        sessionStorage: { getItem: () => 'test-session', setItem: () => undefined } });

});
afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
});
describe('daily panel per-instance polling', () => {
    it.each([200, 503])('skips busy ticks and releases after HTTP %s for the next 15s tick', async (status) => {
        const first = deferred<Response>(), second = deferred<Response>();
        const fetch = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
        vi.stubGlobal('fetch', fetch);
        const cleanup = mount();
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(fetch.mock.calls[0][0]).toBe('/api/onprem/daily-observation');
        await vi.advanceTimersByTimeAsync(45000);
        expect(fetch).toHaveBeenCalledTimes(1);
        const body = status === 200 ? { available: true, snapshotId: 'original-snapshot' } : { error: 'fixture unavailable' };
        first.resolve(response(status, body));
        await vi.advanceTimersByTimeAsync(0);
        if (status === 200)
            expect(hooks.setters[0]).toHaveBeenCalledWith(body);
        else
            expect(hooks.setters[2]).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(14999);
        expect(fetch).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(1);
        expect(fetch).toHaveBeenCalledTimes(2);
        cleanup();
        second.resolve(response(200, { available: true, snapshotId: 'later' }));
        await vi.advanceTimersByTimeAsync(0);
    });
    it('unmount drains the pending response without state updates or further requests', async () => {
        const pending = deferred<Response>();
        const fetch = vi.fn().mockReturnValue(pending.promise);
        vi.stubGlobal('fetch', fetch);
        const cleanup = mount();
        cleanup();
        pending.resolve(response(200, { available: true, snapshotId: 'old-instance' }));
        await vi.advanceTimersByTimeAsync(45000);
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(hooks.setters.every((setter) => setter.mock.calls.length === 0)).toBe(true);
        expect(fetch.mock.calls[0][1]).not.toHaveProperty('signal');
    });
});
