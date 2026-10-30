import type { IntentOperation } from '../../../services/intents'

const verdicts: Record<string, string> = {
  passed: '回報通過', failed: '回報未通過', unknown: '尚未確認', unverified: '尚未驗證',
  pending: '等待確認', not_required: '不需執行', not_applicable: '不適用', restored: '回報已恢復',
  not_comparable: '不可比較',
}
const roles: Record<string, string> = { planner: '規劃', executor: '執行', validator: '驗證' }
/** Backend verdicts describe this operation; they do not extend receiver proof freshness. */
export function OperationControlSummary({ operation }: { operation: IntentOperation }) {
  const hasVerdicts = ['installation_verdict', 'routing_verdict', 'sla_verdict', 'restore_status']
    .some(key => typeof operation[key as keyof IntentOperation] === 'string')
  if (!hasVerdicts && !operation.control) return null
  const fields = [
    ['資源安裝', operation.installation_verdict], ['實際資料路徑', operation.routing_verdict],
    ['接收端 SLA', operation.sla_verdict], ['恢復狀態', operation.restore_status],
  ]
  return <section data-testid="operation-control-summary" className="rounded border border-slate-600 p-3 text-sm">
    <h4 className="font-semibold">本次正式控制操作回報</h4>
    {operation.control?.role && <p className="mt-2 text-slate-300">目前階段：{roles[operation.control.role] ?? '尚未確認'}</p>}
    <dl className="mt-2 grid gap-2 sm:grid-cols-2">{fields.map(([label, value]) => <div key={label}>
      <dt className="text-slate-400">{label}</dt><dd>{verdicts[String(value)] ?? '尚未確認'}</dd>
    </div>)}</dl>
    {operation.pending === true && <p className="mt-2 text-amber-200">操作仍待確認，尚非最終結果。</p>}
    {operation.recovery_required === true && <p className="mt-2 text-amber-200">需要恢復處理；資源釋放仍須由後端確認。</p>}
    <p className="mt-2 text-xs text-slate-400">這是本次操作的後端回報；目前服務是否達標，仍以上方有效期間與新鮮量測證據判定。</p>
  </section>
}
