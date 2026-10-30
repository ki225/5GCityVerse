import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
// Explicit component isolation: no bootstrap HTTP response or native success is fabricated.
// The gate refuses to render its children; the real main App must still expose its sibling dialog.
vi.mock('./components/LearningLab/LearningLab', () => ({ LearningLab: () => null }));
vi.mock('./components/LearningCenter/LearningCenter', () => ({ LearningCenter: () => null }));
vi.mock('./onprem/components/OnpremRuntime/CityBootstrapGate', () => ({ CityBootstrapGate: () => createElement('div', null, 'CITY_NOT_STARTED') }));
import App from './App';
beforeEach(() => {

    vi.stubEnv('VITE_API_URL', 'http://127.0.0.1:8000');
    vi.stubEnv('VITE_WS_URL', 'ws://127.0.0.1:8000/ws');

});
afterEach(() => vi.unstubAllEnvs());
it('actual main App exposes read-only intent entry even when city children are gated', () => {
    const html = renderToStaticMarkup(createElement(App));
    expect(html).toContain('CITY_NOT_STARTED');
    expect(html).toContain("服務意圖");
    expect(html).toContain("以需求 ID 唯讀查詢");
});
