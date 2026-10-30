import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const harness = vi.hoisted(() => ({
    request: vi.fn(),
    effects: [] as Array<() => void | (() => void)>,
    setters: [] as Array<ReturnType<typeof vi.fn>>,
    values: new Map<number, unknown>(),
}));
vi.mock('react', async (importOriginal) => {
    const actual = await importOriginal<typeof import('react')>();
    return {
        ...actual,
        useState: (initial: unknown) => {
            const value = harness.values.has(harness.setters.length) ? harness.values.get(harness.setters.length) : initial;
            const setter = vi.fn();
            harness.setters.push(setter);
            return [value, setter];
        },
        useRef: (initial: unknown) => ({ current: initial }),
        useCallback: (callback: unknown) => callback,
        useEffect: (effect: () => void | (() => void)) => { harness.effects.push(effect); },
    };
});
vi.mock('../onpremBootstrap', async (importOriginal) => ({
    ...await importOriginal<typeof import('../onpremBootstrap')>(),
    requestBootstrapStatus: harness.request,
}));
import { CityBootstrapGate } from '../../components/OnpremRuntime/CityBootstrapGate';
import { BootstrapRequestError, type BootstrapStatus } from '../onpremBootstrap';
const temporaryFailure: BootstrapStatus = { state: 'failed', stage: 'ue', message: 'UE verification unavailable', retryable: true };
const ready: BootstrapStatus = { state: 'ready', stage: 'pdu', message: 'City ready', retryable: false };
async function flushPoll() {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
}
beforeEach(() => {
    vi.useFakeTimers();
    harness.request.mockReset();
    harness.effects.length = 0;
    harness.setters.length = 0;
    harness.values.clear();
});
afterEach(() => { vi.useRealTimers(); });
describe('CityBootstrapGate polling', () => {
    it('revokes previous ready/operator hints while a manual refresh GET is pending and keeps children mounted', () => {
        harness.values.set(0, ready);
        harness.values.set(3, true); // checking
        harness.values.set(5, true); // old operator view
        const tree = CityBootstrapGate({ children: 'Preserved draft and release controls' });
        expect(tree.props.value).toBe(false);
        expect(JSON.stringify(tree.props.children)).toContain('Preserved draft and release controls');
    });
    it('recovers from backend restart transport failure without a POST or unmounting the draft', async () => {
        harness.request.mockRejectedValueOnce(new TypeError('Failed to fetch')).mockResolvedValue(ready);
        CityBootstrapGate({ children: 'City dashboard' });
        const cleanup = harness.effects[0]();
        await flushPoll();
        expect(harness.setters[1]).toHaveBeenCalledWith(expect.stringContaining('無法連線'));
        await vi.advanceTimersByTimeAsync(2000);
        expect(harness.request).toHaveBeenCalledTimes(2);
        expect(harness.setters[0]).toHaveBeenLastCalledWith(ready);
        expect(harness.setters[1]).toHaveBeenLastCalledWith('');
        expect(harness.request.mock.calls.every(call => call[0] === 'GET')).toBe(true);
        if (typeof cleanup === 'function') cleanup();
    });
    it('bounds transport retries and cancels future reads when the gate is disposed', async () => {
        harness.request.mockRejectedValue(new TypeError('Failed to fetch'));
        CityBootstrapGate({ children: 'City dashboard' });
        const cleanup = harness.effects[0]();
        await flushPoll();
        await vi.advanceTimersByTimeAsync(182000);
        const calls = harness.request.mock.calls.length;
        expect(calls).toBeGreaterThan(1);
        expect(calls).toBeLessThanOrEqual(91);
        await vi.advanceTimersByTimeAsync(10000);
        expect(harness.request).toHaveBeenCalledTimes(calls);
        if (typeof cleanup === 'function') cleanup();
        await vi.advanceTimersByTimeAsync(2000);
        expect(harness.request).toHaveBeenCalledTimes(calls);
    });
    it('revokes an existing ready bootstrap after normal fleet retirement', async () => {
        const empty: BootstrapStatus = { state: 'absent', stage: 'ue', message: 'No active fleet', retryable: true };
        harness.request.mockResolvedValueOnce(ready).mockResolvedValueOnce(empty).mockResolvedValue(empty);
        CityBootstrapGate({ children: 'City dashboard' });
        const cleanup = harness.effects[0]();
        await flushPoll();
        await vi.advanceTimersByTimeAsync(2000);
        expect(harness.setters[0]).toHaveBeenLastCalledWith(empty);
        expect(harness.request).toHaveBeenCalledTimes(2);
        if (typeof cleanup === 'function') cleanup();
    });
    it('rechecks a retryable failure and clears the previous error when the same bootstrap recovers', async () => {
        harness.request.mockResolvedValueOnce(temporaryFailure).mockResolvedValueOnce(ready);
        CityBootstrapGate({ children: 'City dashboard' });
        const cleanup = harness.effects[0]();
        await flushPoll();
        expect(harness.request).toHaveBeenCalledTimes(1);
        expect(harness.setters[0]).toHaveBeenCalledWith(temporaryFailure);
        await vi.advanceTimersByTimeAsync(2000);
        await flushPoll();
        expect(harness.request).toHaveBeenCalledTimes(2);
        expect(harness.setters[0]).toHaveBeenCalledWith(ready);
        expect(harness.setters[1]).toHaveBeenCalledWith('');
        if (typeof cleanup === 'function')
            cleanup();
    });
    it('stops polling a permanent failure', async () => {
        harness.request.mockResolvedValue({ ...temporaryFailure, retryable: false });
        CityBootstrapGate({ children: 'City dashboard' });
        const cleanup = harness.effects[0]();
        await flushPoll();
        await vi.advanceTimersByTimeAsync(4000);
        expect(harness.request).toHaveBeenCalledTimes(1);
        if (typeof cleanup === 'function')
            cleanup();
    });
    it('rechecks a retryable non-2xx status and clears its error after recovery', async () => {
        harness.request.mockRejectedValueOnce(new BootstrapRequestError(temporaryFailure.message, temporaryFailure))
            .mockResolvedValueOnce(ready);
        CityBootstrapGate({ children: 'City dashboard' });
        const cleanup = harness.effects[0]();
        await flushPoll();
        expect(harness.setters[1]).toHaveBeenCalledWith(temporaryFailure.message);
        await vi.advanceTimersByTimeAsync(2000);
        await flushPoll();
        expect(harness.request).toHaveBeenCalledTimes(2);
        expect(harness.setters[0]).toHaveBeenCalledWith(ready);
        expect(harness.setters[1]).toHaveBeenLastCalledWith('');
        if (typeof cleanup === 'function')
            cleanup();
    });
});
