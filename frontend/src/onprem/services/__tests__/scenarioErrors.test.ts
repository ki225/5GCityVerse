import { afterEach, describe, expect, it, vi } from 'vitest';
import { getScenarioRound, scenarioErrorText, ScenarioHttpError } from '../onpremScenarios';
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
describe('safe persistent scenario rejection diagnostics', () => {
    it('renders known conflict without exposing server text', () => {
        const known = new ScenarioHttpError(409, { error: 'ROUND_CONFLICT', detail: 'Fresh known-scope resource baseline is unavailable' });
        expect(scenarioErrorText(known)).toContain('資源基準');
        const arbitrary = new ScenarioHttpError(409, { error: 'ROUND_CONFLICT', detail: 'SECRET<script>' });
        expect(scenarioErrorText(arbitrary)).toContain('輪次狀態衝突');
        expect(scenarioErrorText(arbitrary)).not.toContain('SECRET');
        expect(scenarioErrorText(new ScenarioHttpError(403, { error: 'SECRET', detail: 'SECRET' }))).toContain('403');
    });
    it.each([
        [409, JSON.stringify({ error: 'ROUND_CONFLICT', detail: 'Fresh known-scope resource baseline is unavailable' }), true],
        [409, JSON.stringify({ error: 'ROUND_CONFLICT', detail: 'SECRET'.repeat(1000) }), false],
        [409, JSON.stringify({ error: 'UNKNOWN', detail: 'SECRET' }), false],
        [403, JSON.stringify({ error: 'ROUND_CONFLICT', detail: 'SECRET' }), false],
        [409, 'not json SECRET', false],
    ] as const)('keeps HTTP rejection classification for %s', async (status, raw, baseline) => {

        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status, text: async () => raw }));
        const error = await getScenarioRound().catch((cause: unknown) => cause);
        expect(error).toBeInstanceOf(ScenarioHttpError);
        expect((error as ScenarioHttpError).definitive).toBe(true);
        expect((error as ScenarioHttpError).baselineUnavailable).toBe(baseline);
        expect(scenarioErrorText(error)).not.toContain('SECRET');
    });
});
