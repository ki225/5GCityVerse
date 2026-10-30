import type { OperationDetailView, ServiceIntentView } from '../../../services/intents';
import { intentDetail } from '../../../services/intents';
import { intentEvidence, record, rows, scalar } from './intentPresentation';
import { OperationControlSummary } from './OperationControlSummary';
export function OperationDetails({ operation }: {
    operation: OperationDetailView;
}) {
    const facts = [
        ['操作 ID', operation.operation_id],
        ['狀態', operation.status], ['種類', operation.kind],
        ['階段', operation.stage], ['錯誤代碼', operation.error_code],
        ['錯誤分類', operation.category],
        ['讀回原因', operation.readback_reason],
    ];
    return <div data-testid="exact-operation-details" className="mt-3 rounded border border-slate-600 p-3">
    <dl className="space-y-2 text-sm">{facts.map(([label, value]) => <div key={label} className="break-all"><dt className="text-slate-400">{label}</dt><dd>{value ?? ('未提供安全代碼')}</dd></div>)}</dl>
    <p className="mt-3 text-sm text-amber-200">{'此為單次操作的獨立紀錄；不作為目前配置生效或服務達標證據。'}</p>
    {operation.error_code === 'model_proposal_unavailable' && <p className="mt-2 text-sm">{'當次模型規劃無法完成；尚未驗證該操作改善服務。'}</p>}
  </div>;
}
export function IntentReport({ intent, memberPage, now }: {
    intent: ServiceIntentView;
    memberPage?: {
        items: unknown[];
        total: number;
        rejected: unknown[];
    };
    now?: number;
}) {
    const op = intent.latest_operation;
    const membership = record(intent.membership);
    const members = rows(membership.members);
    const evidence = intentEvidence(intent, now);
    const rejected = evidence.rejected;
    const visibleMembers = memberPage ? rows(memberPage.items) : members;
    const effect = op?.effect ?? {};
    const pendingCount = typeof membership.pending_count === 'number'
        ? membership.pending_count : rows(membership.pending_members).length;
    const currentEffect = evidence.satisfied;
    const actions = rows(op?.plan?.actions);
    const receipts = rows(op?.receipts);
    const metricsBefore = rows(op?.before?.metrics);
    const metricsAfter = rows(op?.after?.metrics);
    const actionNames: Record<string, string> = {
        update_qos: '調整流量政策', resize_nf: '調整網元 CPU／RAM', add_upf: '增加 UPF',
        assign_sessions: '分配連線', provision_node: '新增節點', place_nf: '部署網元', drain_and_remove: '排空並回收',
        place_application: '部署應用', establish_sessions: '建立連線',
        bind_application_endpoint: '綁定應用端點', drain_application_endpoint: '排空應用端點',
        start_service_flows: '啟動服務流量', reestablish_sessions: '重建連線',
    };
    const facts = [
        ['意圖來源', scalar(intent.provenance)],
        ['符合條件成員', String(memberPage?.total ?? members.length)],
        ['已安裝政策成員', scalar(membership.installed_count)],
        ['待驗證成員', String(pendingCount)],
        ['未納入成員', String(rejected.length)],
        ['操作收據', String(receipts.length)],
        ['預估成本（依配置預算單位）', scalar(op?.plan?.cost)],
        ['服務目標', currentEffect && effect.met === true ? ('已驗證達標') : ('尚未驗證達標')],
        ['其他服務保障', currentEffect && effect.protected_services_met === true ? ('本次檢查通過') : ('尚未驗證')],
    ];
    const milestoneLabels = ['已理解需求', '已產生計畫', '配置已生效', '服務已達標'];
    const stateLabels = { confirmed: '證據已確認', historical: '歷史證據（非目前狀態）', unverified: '尚未驗證', not_required: '維持原配置，無修改' };
    const modelFields = ['provider', 'model', 'response_received', 'request_id', 'response_id'];
    const sourceModel = modelFields.filter(name => evidence.model[name] !== undefined);
    return <div className="mt-5 space-y-5">
    <section aria-label={'需求四階段證據'}>
      <ol className="grid gap-2 sm:grid-cols-2">{evidence.milestones.map((step, i) => <li key={step.key} data-evidence-stage={step.key} data-evidence-state={step.state} className="rounded border border-slate-600 p-3 text-sm"><b>{milestoneLabels[i]}</b><span className="mt-1 block">{stateLabels[step.state]}</span><span className="mt-1 block break-all text-xs text-slate-400">{step.reason}</span></li>)}</ol>
      <p className="mt-2 text-xs text-slate-400">{'每階段依各自的服務端證據判定；歷史配置不代表現在仍生效。'}</p>
    </section>
    {op && <OperationControlSummary operation={op} />}
    <section><h4 className="font-semibold">{'模型來源與判斷依據'}</h4>
      {sourceModel.length ? <dl className="mt-2 text-sm">{sourceModel.map(name => <div key={name} className="break-all"><dt className="inline text-slate-400">{name}: </dt><dd className="inline">{typeof evidence.model[name] === 'boolean' ? String(evidence.model[name]) : scalar(evidence.model[name])}</dd></div>)}</dl> : <p className="mt-2 text-sm text-amber-200">{'尚無模型來源紀錄，不能宣稱 AI 已判斷；人工或基準契約亦可能沒有模型呼叫。'}</p>}
      <p className="mt-2 text-xs text-slate-400">{'以下為可追溯的計畫證據與不確定性，並非模型的內部思考紀錄。'}</p>
      {['evidence', 'uncertainty'].map(name => <div key={name} className="mt-2 text-sm"><b>{name === 'evidence' ? ('瓶頸／計畫依據') : ('不確定性與限制')}</b><ul className="mt-1 list-inside list-disc">{Array.isArray(op?.plan?.[name]) && (op?.plan?.[name] as unknown[]).length ? (op?.plan?.[name] as unknown[]).map((value, index) => <li key={index} className="break-all">{intentDetail(value) || scalar(value)}</li>) : <li className="text-slate-400">{'尚無服務端紀錄'}</li>}</ul></div>)}
      {op?.error != null && <p className="mt-2 break-all text-sm text-amber-200">{intentDetail(op.error) || ('服務端未提供可讀原因')}</p>}
    </section>
    <section><h4 className="font-semibold">{'需求與來源'}</h4>
      <dl className="mt-2 space-y-1 text-sm">{facts.map(([label, value]) => <div key={label} className="flex flex-wrap justify-between gap-x-3"><dt className="text-slate-400">{label}</dt><dd className="break-all">{value}</dd></div>)}</dl>
      <ul className="mt-3 space-y-2 text-sm">{rows(intent.objectives).map((o, i) => <li key={i} className="rounded border border-slate-700 p-2">
        {scalar(o.metric)} {o.direction === 'at_most' ? '≤' : '≥'} {scalar(o.target)} {scalar(o.unit)}
        <span className="block text-slate-400">{scalar(o.statistic)} · {scalar(o.scope)} · {scalar(o.window_seconds)} s · {scalar(o.provenance)}</span>
      </li>)}</ul>
    </section>
    <section><h4 className="font-semibold">{'實際服務成員（session／flow）'}</h4>
      {visibleMembers.length === 0 && <p className="mt-2 text-sm text-slate-400">{'本頁尚無已確認成員，不能以 UE 數推算 sessions。'}</p>}
      <ul className="mt-2 space-y-2 text-xs">{visibleMembers.map((member, i) => <li key={i} className="break-all rounded border border-slate-700 p-2"><span className="block">session: {scalar(member.session_token ?? member.session_key)}</span><span className="block">flow: {scalar(member.flow_id)}</span></li>)}</ul>
    </section>
    <section><h4 className="font-semibold">{'資源調整與原生收據'}</h4>
      {actions.length === 0 && <p className="mt-2 text-sm text-slate-400">{op?.plan
                ? ('計畫維持現有配置，服務達標與否以接收端量測為準。')
                : ('尚無資源調整計畫。')}</p>}
      <ul className="mt-2 space-y-2 text-sm">{actions.map((action, i) => <li key={i} className="rounded border border-slate-700 p-2">
        {actionNames[scalar(action.kind)] ?? scalar(action.kind)} · {scalar(action.target_id)}
        <span className="mt-1 block text-slate-400">{'相應收據'}：{scalar(receipts.find(r => r.action_id === action.action_id)?.state)}</span>
        <details className="mt-2"><summary className="cursor-pointer">{'期望配置與實際配置'}</summary>
          <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify({ requested: action.parameters, installed: receipts.find(r => r.action_id === action.action_id)?.installed ?? null }, null, 2)}</pre>
        </details>
      </li>)}</ul>
    </section>
    <section><h4 className="font-semibold">{'接收端前後量測'}</h4>
      <p className="mt-2 break-all text-xs text-slate-400">{'原始觀測時間（epoch 秒）'}: {scalar(op?.before?.observed_at)} → {scalar(op?.after?.observed_at)} · observation: {scalar(op?.after?.observation_id)}</p>
      <div className="mt-2 max-h-64 overflow-auto"><table className="w-full text-left text-xs"><thead><tr><th className="p-2">{'對象／指標'}</th><th className="p-2">{'修改前'}</th><th className="p-2">{'修改後'}</th></tr></thead>
        <tbody>{metricsAfter.filter(m => ['ul_throughput', 'dl_throughput', 'rtt', 'loss', 'demand_satisfaction'].includes(scalar(m.name))).map((m, i) => {
            const before = metricsBefore.find(b => b.resource_id === m.resource_id && b.name === m.name && b.statistic === m.statistic && b.scope === m.scope);
            return <tr key={i} className="border-t border-slate-700"><td className="max-w-48 break-all p-2">{scalar(m.resource_id)}<br />{scalar(m.name)} ({scalar(m.unit)})</td><td className="p-2">{scalar(before?.value)}</td><td className="p-2">{m.validity === 'valid' ? scalar(m.value) : ('無有效量測')}</td></tr>;
        })}</tbody></table></div>
      {metricsAfter.length === 0 && <p className="mt-2 text-sm text-slate-400">{'尚無修改後量測，不能判定服務改善。'}</p>}
    </section>
    {rejected.length > 0 && <section><h4 className="font-semibold">{'無法套用的成員'}</h4><ul className="mt-2 text-sm text-amber-200">{rejected.map((row, i) => <li key={i}>{row.session}: {row.reason}</li>)}</ul></section>}
    <section><h4 className="font-semibold">{'恢復與回收'}</h4><p className="mt-2 text-sm text-slate-300">{'補償收據'}：{op?.compensation?.length ?? 0} · {'回收收據'}：{op?.released?.length ?? 0}</p></section>
  </div>;
}
