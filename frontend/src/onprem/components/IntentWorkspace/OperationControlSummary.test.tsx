import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { OperationControlSummary } from './OperationControlSummary'
import { IntentReport } from './IntentReport'

describe('formal backend control result projection', () => {
  it('separates native installation, routing, SLA and recovery without inventing current success', () => {
    const operation = { operation_id: 'op', intent_id: 'intent', status: 'recovery_required',
      installation_verdict: 'passed', routing_verdict: 'failed', sla_verdict: 'unknown', restore_status: 'pending',
      pending: true, recovery_required: true, control: { nodes: ['planner', 'executor', 'validator'], role: 'validator' } }
    const html = renderToStaticMarkup(<IntentReport intent={{ intent_id: 'intent', revision: 1, latest_operation: operation }} />)
    expect(html).toContain('本次正式控制操作回報')
    for (const text of ['資源安裝', '實際資料路徑', '接收端 SLA', '恢復狀態', '回報未通過', '需要恢復處理']) expect(html).toContain(text)
    expect(html).not.toContain('data-evidence-stage="satisfied" data-evidence-state="confirmed"')
  })

  it('does not expose unexpected backend verdict strings', () => {
    const html = renderToStaticMarkup(<OperationControlSummary operation={{ operation_id: 'op', intent_id: 'i', status: 'running',
      installation_verdict: 'SECRET<script>', control: { role: 'untrusted-worker-name' } }} />)
    expect(html).toContain('尚未確認')
    expect(html).not.toContain('SECRET')
    expect(html).not.toContain('untrusted-worker-name')
  })

  it('keeps earlier backend replies compatible without synthesizing verdicts', () => {
    expect(renderToStaticMarkup(<OperationControlSummary operation={{ operation_id: 'op', intent_id: 'i', status: 'queued' }} />)).toBe('')
  })
})
