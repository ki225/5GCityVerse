import { describe, expect, it } from 'vitest';
import { intentStateLabel } from './IntentWorkspace';
import { intentDetail } from '../../../services/intents';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { IntentReport } from './IntentReport';
describe('intent progress evidence', () => {
    it('does not present a completed operation as service satisfaction', () => {
        expect(intentStateLabel('completed')).toBe('操作完成');
        expect(intentStateLabel('satisfied')).toBe('服務已達標');
        expect(intentStateLabel('recovery_required')).toBe("需要恢復處理");
    });
    it('shows structured validation reasons and network errors', () => {
        expect(intentDetail([{ reason: 'stale observation' }, { message: 'missing RTT' }])).toBe('stale observation; missing RTT');
        expect(intentDetail(new Error('offline'))).toBe('offline');
        expect(intentDetail(null)).toBe('');
    });
    it('renders absent native proof as unverified and escapes supplied content', () => {
        const html = renderToStaticMarkup(createElement(IntentReport, {
            intent: { intent_id: 'i', revision: 1, provenance: '<script>bad()</script>',
                latest_operation: { intent_id: 'i', operation_id: 'o', status: 'completed' } },
        }));
        expect(html).toContain('尚未驗證達標');
        expect(html).toContain('尚無修改後量測');
        expect(html).toContain('&lt;script&gt;');
        expect(html).not.toContain('<script>');
    });
    it('invalidates a prior successful effect when the current group gains a pending member', () => {
        const base = { intent_id: 'i', revision: 1,
            latest_operation: { intent_id: 'i', operation_id: 'old', status: 'completed',
                effect: { met: true, protected_services_met: true } } };
        for (const status of ['active', 'satisfied']) {
            const html = renderToStaticMarkup(createElement(IntentReport, {
                intent: { ...base, status, membership: { members: [{}, {}], installed_count: 2,
                        pending_count: 1, pending_members: [{}], effect_invalidated: true } } }));
            expect(html).toContain('待驗證成員');
            expect(html).toContain('尚未驗證達標');
            expect(html).not.toContain('已驗證達標');
            expect(html).not.toContain('本次檢查通過');
        }
        const verified = renderToStaticMarkup(createElement(IntentReport, {
            intent: { ...base, status: 'satisfied', membership: { pending_count: 0, effect_invalidated: false } } }));
        // Bare booleans omit the receiver observation and original window; they cannot establish effect.
        expect(verified).toContain('尚未驗證達標');
        expect(verified).not.toContain('本次檢查通過');
    });
    it('presents a HOLD as a plan while keeping service verification separate', () => {
        for (const status of ['active', 'satisfied']) {
            const html = renderToStaticMarkup(createElement(IntentReport, {
                intent: { intent_id: 'i', revision: 1, status, membership: { pending_count: 0 },
                    latest_operation: { intent_id: 'i', operation_id: 'hold', status: 'completed',
                        plan: { actions: [] }, effect: { met: true, protected_services_met: true } } } }));
            expect(html).toContain('計畫維持現有配置');
            expect(html).not.toContain('尚無資源調整計畫');
            expect(html).toContain('尚未驗證達標');
        }
    });
});
