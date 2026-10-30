import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { actualSessionCount, intentEvidence, rejectedMembers } from './intentPresentation';
import { IntentReport } from './IntentReport';
import type { ServiceIntentView } from '../../../services/intents';
const now = Date.now() / 1000;
function verified(): ServiceIntentView {
    return { intent_id: 'i', revision: 2, status: 'satisfied', provenance: 'user', selector: { service_types: ['video'] },
        valid_from: now - 10, valid_until: now + 600, objectives: [{ metric: 'dl_throughput', target: 8000000, provenance: 'user' }],
        membership: { members: [{ session_token: 'video', flow_id: '' }], pending_count: 0 },
        latest_operation: { intent_id: 'i', operation_id: 'op', status: 'completed',
            model_provenance: { provider: 'openai', model: 'real-model', response_received: true },
            plan: { plan_id: 'p', intent_id: 'i', intent_revision: 2, actions: [], evidence: ['receiver bottleneck evidence'], uncertainty: ['host CPU not calibrated'] },
            after: { observation_id: 'a', observed_at: now, maximum_age_seconds: 30, metrics: [] },
            effect: { met: true, protected_services_met: true, same_membership: true, fresh_window: true, owner_id: 'i', operation_id: 'op',
                source: 'current-native-receiver', observation_id: 'a', observed_at: now, objectives: [{ status: 'passed' }] } } };
}
describe('separate durable milestone evidence', () => {
    it('represents a valid current hold without inventing native modifications', () => {
        const value = intentEvidence(verified(), now);
        expect(value.milestones.map(m => m.state)).toEqual(['confirmed', 'confirmed', 'not_required', 'confirmed']);
        expect(value.satisfied).toBe(true);
    });
    it.each(['expired', 'released', 'recovery_required', 'failed', 'compensating', 'cancelled', 'notaccepted'])('never makes inactive/recovery %s currently satisfied', state => {
        const value = verified();
        if (['expired', 'released'].includes(state))
            value.status = state;
        else
            value.latest_operation!.status = state;
        expect(intentEvidence(value, now).satisfied).toBe(false);
    });
    it.each(['missing_after', 'stale_after', 'foreign_op', 'foreign_observation', 'pending', 'invalidated', 'missing_objectives', 'clock_future', 'future_contract', 'missing_contract', 'unbounded_age'])('refuses incomplete or changed receiver proof: %s', damage => {
        const value = verified(), op = value.latest_operation!;
        if (damage === 'missing_after')
            delete op.after;
        if (damage === 'stale_after')
            op.after!.observed_at = now - 31;
        if (damage === 'foreign_op')
            op.effect!.operation_id = 'foreign';
        if (damage === 'foreign_observation')
            op.effect!.observation_id = 'foreign';
        if (damage === 'pending')
            (value.membership as Record<string, unknown>).pending_count = 1;
        if (damage === 'invalidated')
            (value.membership as Record<string, unknown>).effect_invalidated = true;
        if (damage === 'missing_objectives')
            op.effect!.objectives = [];
        if (damage === 'clock_future')
            op.after!.observed_at = now + 2;
        if (damage === 'future_contract')
            value.valid_from = now + 10;
        if (damage === 'missing_contract')
            delete value.valid_until;
        if (damage === 'unbounded_age')
            op.after!.maximum_age_seconds = Infinity;
        expect(intentEvidence(value, now).satisfied).toBe(false);
    });
    it('does not infer understanding or a plan from an API completion or model call', () => {
        const value = verified();
        value.objectives = [];
        value.latest_operation!.plan = { actions: [] };
        const milestones = intentEvidence(value, now).milestones;
        expect(milestones[0].state).toBe('unverified');
        expect(milestones[1].state).toBe('unverified');
    });
    it('preserves historical installed receipts after release without current success', () => {
        const value = verified();
        value.status = 'expired';
        value.latest_operation!.plan!.actions = [{ action_id: 'a', kind: 'update_qos' }];
        value.latest_operation!.receipts = [{ action_id: 'a', state: 'released', installed: { version: 'actual' }, installation_confirmed_at: now - 1 }];
        const evidence = intentEvidence(value, now);
        expect(evidence.milestones[2].state).toBe('historical');
        expect(evidence.satisfied).toBe(false);
    });
});
describe('actual API DTO and provenance rendering', () => {
    it('renders actual rejected [session, reason] tuple without empty projection', () => {
        expect(rejectedMembers([['session-1', 'unsupported_granularity']])).toEqual([{ session: 'session-1', reason: 'unsupported_granularity' }]);
        const value = verified();
        value.membership = { members: [], rejected: [['session-1', 'unsupported_granularity']] };
        const html = renderToStaticMarkup(createElement(IntentReport, { intent: value, memberPage: { items: [{ session_token: 'two-PDU-video', flow_id: 'qfi9' }], total: 101, rejected: [] } }));
        expect(html).toContain('session-1: unsupported_granularity');
        expect(html).toContain('two-PDU-video');
        expect(html).toContain('qfi9');
        expect(html).toContain('101');
        expect(html).toContain('real-model');
        expect(html).toContain('receiver bottleneck evidence');
        expect(html).toContain('host CPU not calibrated');
    });
    it('released baseline with no model source never fabricates AI or current service success', () => {
        const value = verified();
        value.status = 'expired';
        delete value.latest_operation!.model_provenance;
        const html = renderToStaticMarkup(createElement(IntentReport, { intent: value }));
        expect(html).toContain('尚無模型來源紀錄');
        expect(html).toContain("尚未驗證");
        expect(html).not.toContain('real-model');
    });
    it('escapes untrusted server evidence and does not render unknown model credential properties', () => {
        const value = verified();
        value.latest_operation!.model_provenance = { provider: '<script>x</script>', secret: 'do-not-render' };
        const html = renderToStaticMarkup(createElement(IntentReport, { intent: value }));
        expect(html).toContain('&lt;script&gt;');
        expect(html).not.toContain('<script>');
        expect(html).not.toContain('do-not-render');
    });
});
describe('physical UE aggregates are not session counts', () => {
    it('uses two unique actual sessions, independently of any aggregate UE count', () => {
        expect(actualSessionCount([{ sessionKey: 'pdu-one' }, { sessionKey: 'pdu-two' }], true)).toBe(2);
        expect(actualSessionCount([], true)).toBe(0);
    });
    it.each([undefined, null, [{ sessionId: 1 }], [{ sessionKey: 'same' }, { sessionKey: 'same' }]])('keeps missing/ambiguous session proof unknown: %s', value => {
        expect(actualSessionCount(value, true)).toBeNull();
    });
    it('cannot use stale perSession records as current count', () => expect(actualSessionCount([{ sessionKey: 'one' }], false)).toBeNull());
});
