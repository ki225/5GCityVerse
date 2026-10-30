import { useEffect, useState, type ReactNode } from 'react';
import { freshTimestamp, receiverEvidenceCoverage, verifiedReceiverMeasurements, type ScenarioGroup } from '../../services/onpremScenarios';
import { qosSampleFresh, qosMetricTarget, qosMetricVerdict, type AgentRole, type QosMetric, type QosSample, type RoundChange, type QerEvidenceSummary } from '../../services/roundObservability';
import { useOnpremScenario, type ScenarioAdapter } from './ScenarioContext';
import { OnpremPolicyPanel, OnpremRoundEvidence, OnpremRoundOutcome, RoundCleanupControl } from './ScenarioPanels';
import { actionRelatedPanels, roundActions, scopedActions, selectedActivityAction, type ActionSummary, type ActionResource } from '../../services/actionEvidence';
import { useActionEvidence, useActionLogs } from './useActionEvidence';
import { DailyNativeObservationPanel } from './DailyNativeObservationPanel';
import { QosTimeline, observationTime } from './QosTimeline';
import { useFormalIntent } from '../../../city-map/context/FormalIntentContext';
import { formalAgentControl } from '../../services/formalAgentControl';
export { measurementSegments, observationTime } from './QosTimeline';
const card = 'rounded-xl border border-slate-200 bg-white p-3';
const value = (n: number | null | undefined, unit = '') => n == null ? '—' : `${n.toFixed(1)}${unit}`;
export function EvidenceDisclosure({ title, children, onOpen }: {
    title: string;
    children: ReactNode;
    onOpen?: (open: boolean) => void;
}) {
    return <details onToggle={(event) => onOpen?.(event.currentTarget.open)} className="rounded-lg border border-slate-200 bg-white text-xs"><summary className="cursor-pointer px-3 py-2 font-semibold text-slate-700">{title}</summary><div className="max-h-96 space-y-2 overflow-auto border-t border-slate-100 p-3">{children}</div></details>;
}
export function selectedQosSample(adapter: ScenarioAdapter): QosSample | null {
    const scenario = adapter.selectedScenario ?? 'web_browsing';
    if (adapter.selectedSampleId)
        return adapter.observation?.samples.find((s) => s.id === adapter.selectedSampleId && s.scenario === scenario) ?? null;
    return adapter.observation?.data?.latestQos.find((s) => s.scenario === scenario) ?? null;
}
export function SliceQosSummary({ adapter, title }: {
    adapter: ScenarioAdapter;
    title: string;
}) {
    const sample = selectedQosSample(adapter);
    const savedFinal = !adapter.selectedSampleId && adapter.snapshot?.stage === 'completed' && Boolean(sample
        && sample.roundId === adapter.snapshot.roundId && sample.generation === adapter.snapshot.generation);
    const historical = Boolean(adapter.selectedSampleId) || savedFinal;
    const fresh = !adapter.observation?.error && qosSampleFresh(sample, adapter.now);
    const rows = adapter.fresh ? verifiedReceiverMeasurements(adapter.snapshot, adapter.now).filter((r) => r.scenario === adapter.selectedScenario) : [];
    const visible = historical || fresh;
    const expired = Boolean(sample?.observedAt && !freshTimestamp(sample.observedAt, adapter.now));
    const rttVisible = visible && (!sample?.rtt || historical || freshTimestamp(sample.rtt.finishedAt, adapter.now));
    const sum = (key: 'receivedUplinkMbps' | 'receivedDownlinkMbps') => rows.some((r) => r[key] !== null) ? rows.reduce((n, r) => n + (r[key] ?? 0), 0) : null;
    const metrics: Array<{
        key: QosMetric;
        label: string;
        unit: string;
        measured: number | null | undefined;
    }> = [
        { key: 'receivedUplinkMbps', label: '接收 UL', unit: 'Mbps', measured: sample ? visible ? sample.receivedUplinkMbps : null : sum('receivedUplinkMbps') },
        { key: 'receivedDownlinkMbps', label: '接收 DL', unit: 'Mbps', measured: sample ? visible ? sample.receivedDownlinkMbps : null : sum('receivedDownlinkMbps') },
        { key: 'rttMs', label: sample?.rtt?.statistic === 'mean' ? '平均 RTT' : 'RTT', unit: 'ms', measured: rttVisible ? sample?.rttMs : null },
        { key: 'lossPercent', label: '接收端丟包', unit: '%', measured: visible ? sample?.lossPercent : null },
    ];
    const count = visible && sample?.quality === 'observed' ? sample.sampleCount : !sample && rows.length ? rows.length : null;
    return <section className={`mt-3 ${card}`} data-testid="slice-qos-summary" data-window-id={sample?.id ?? ''} data-historical={String(historical)} data-source-observed-at={sample?.observedAt ?? ''} data-metric-fresh={String(fresh)}>
    <div className="flex items-center justify-between gap-2"><h3 className="text-sm font-bold">{savedFinal ? '最後一次量測 QoS' : title} · {sample?.slice ?? '—'}</h3>{adapter.selectedSampleId && <button type="button" className="min-h-9 px-2 text-xs text-primary-700" onClick={() => { adapter.selectSample?.(null); adapter.selectDecision?.(null); }}>{'回到最新'}</button>}</div>
    <p className={`mt-1 text-[10px] ${historical || !fresh ? 'text-amber-800' : 'text-slate-500'}`}>{savedFinal ? '測試已結束；顯示保存的歷史量測，非目前流量' : historical ? '歷史窗口' : fresh ? '近期接收端量測' : expired ? '資料已過期，等待新鮮量測' : '等待新鮮量測'}{sample?.observedAt && ` · ${sample.observedAt}`}</p>
    {sample && <p className="mt-1 text-[10px] text-slate-500">{'量測來源'}: {sample.source}</p>}
    <p className="mt-1 text-[10px] text-slate-500" data-testid="qos-scope">{'範圍：所選情境總量'} · {'有效 UE'}: {count ?? '—'}</p>
    <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">{metrics.map(({ key, label, unit, measured }) => {
            const target = qosMetricTarget(sample, key);
            const status = adapter.observation?.error && !historical ? 'unknown' : qosMetricVerdict(sample, key, adapter.now, historical);
            const measurementState = measured != null ? 'observed' : sample && (!visible || (key === 'rttMs' && !rttVisible)) ? 'stale' : sample ? 'not_measured' : 'no_window';
            return <div key={key} className="rounded-lg border border-slate-100 bg-slate-50 p-2" data-testid={`qos-metric-${key}`} data-verdict={status} data-measurement-state={measurementState}>
        <small className="text-[10px] text-slate-500">{label}</small><b className="mt-1 block text-sm text-primary-800">{value(measured, ` ${unit}`)}</b>
        {measured == null && <small className="mt-1 block text-[9px] text-slate-500">{measurementState === 'stale' ? '量測已過期' : measurementState === 'not_measured' ? '此窗口未量測' : '此情境尚無量測窗口'}</small>}
        <small className="mt-1 block text-[9px] text-slate-500">{'目標'}: {adapter.snapshot?.serviceRequirements ? '見服務 SLA（獨立範圍與視窗）' : target === null ? '未設定' : `${key.startsWith('received') ? '≥' : '≤'} ${value(target, ` ${unit}`)}`}</small>
        <span className={`mt-1 block text-[10px] ${status === 'met' ? 'text-emerald-700' : status === 'not_met' ? 'text-red-700' : 'text-slate-500'}`}>{status === 'met' ? '達標' : status === 'not_met' ? '未達標' : '未判定'}</span>
        {key === 'rttMs' && sample?.rtt?.sameTrafficWindow === false && <small className="mt-1 block text-[9px] text-slate-500">{'獨立探測（相鄰窗口）'} · {sample.rtt.validUeCount ?? '—'} / {sample.rtt.expectedUeCount ?? '—'} UE<br />{sample.rtt.finishedAt ?? '—'}</small>}
      </div>;
        })}</div>
    {sample?.targets && <p className="mt-2 text-[10px] text-slate-500">{'實驗目標版本'}: {sample.targets.revision} · {sample.targets.source}</p>}
  </section>;
}
function RoundStatus({ adapter }: {
    adapter: ScenarioAdapter;
}) {
    const s = adapter.snapshot;
    const coverage = receiverEvidenceCoverage(s, adapter.fresh ? adapter.now : Number.POSITIVE_INFINITY);
    const trafficCount = coverage.status === 'complete' && freshTimestamp(s?.trafficCounts?.observedAt, adapter.now) ? s?.trafficCounts?.observedTransmitting : null;
    return <section className={card} data-testid="network-ai-round-status"><div className="mb-2 flex justify-between gap-2"><h2 className="text-sm font-bold">{'本輪狀態'}</h2><span className="text-xs text-primary-700">{s?.stage ?? '—'}</span></div><OnpremRoundOutcome snapshot={s} compact/>
    <dl className="mt-3 grid grid-cols-3 gap-2 text-xs">{[['已註冊', s?.counts.registered], ['已建立 PDU 的 UE', s?.counts.pduEstablished], ['最近量測有流量 UE', trafficCount]].map(([label, n]) => <div key={String(label)}><dt className="text-[10px] text-slate-500">{label}</dt><dd className="mt-1 font-bold">{adapter.fresh ? n ?? '—' : '—'} / {s?.allocation.total ?? '—'}</dd></div>)}</dl>
    {coverage.expected > 0 && <p data-testid="network-ai-receiver-coverage" data-status={coverage.status} data-expected={coverage.expected} className="mt-2 text-xs">
      接收端證據：UL {coverage.uplink} / {coverage.expected} · DL {coverage.downlink} / {coverage.expected} · 雙向 {coverage.duplex} / {coverage.expected}
      {coverage.status === 'unknown' ? '；缺少或過期的證據仍為未知。' : '；覆蓋完整不代表 SLA 達標。'}
    </p>}
    {adapter.error && <p role="alert" className="mt-2 text-xs text-red-700">{adapter.error}</p>}{adapter.uncertain && <p role="alert" className="mt-2 text-xs text-amber-900">{'提交結果待核對，請勿重複送出。'}</p>}
  </section>;
}
export function AgentStatus({ adapter }: {
    adapter: ScenarioAdapter;
}) {
    const observed = adapter.observation?.data?.agent;
    const fallback = adapter.snapshot?.agentRuntime;
    const formal = useFormalIntent();
    const formalMode = Boolean(formal?.submission);
    const formalAgent = formalAgentControl(formal?.active, formal?.submission, adapter.snapshot,
        formal?.activeObservedAt, adapter.now);
    const agent = formalMode ? formalAgent : observed ?? fallback;
    const started = formalMode ? undefined : observed?.startedAt;
    const elapsed = Number.isFinite(observationTime(started)) ? Math.max(0, (adapter.now - observationTime(started)) / 1000) : null;
    const agentTime = observationTime(agent?.updatedAt);
    const stale = formalMode ? !formalAgent : Boolean(adapter.observation?.error) || !Number.isFinite(agentTime) || !freshTimestamp(new Date(agentTime).toISOString(), adapter.now);
    const stepActive = (role: string, phase: string) => agent?.role === role && (role !== 'validator' || agent?.phase === phase);
    return <section className={card} data-testid="network-ai-agent"><h3 className="text-sm font-bold">{'此刻的 AI'}</h3><div className="my-2 flex items-center justify-between gap-1 text-[10px]">{[['planner', 'plan'], ['validator', 'precheck'], ['executor', 'apply'], ['validator', 'postcheck']].map(([role, phase], i) => <button type="button" key={phase} data-testid={`agent-step-${phase}`} data-active={String(stepActive(role, phase))} aria-pressed={adapter.selectedAgentRole === role} onClick={() => adapter.selectAgentRole?.(adapter.selectedAgentRole === role ? null : role as AgentRole)} className={`min-h-8 rounded px-1 ${stepActive(role, phase) ? 'font-bold text-primary-700' : 'text-slate-500'} ${adapter.selectedAgentRole === role ? 'bg-primary-50 ring-1 ring-primary-200' : ''}`}>{i > 0 && <span className="mr-1 text-slate-300">→</span>}{stepActive(role, phase) ? '● ' : '○ '}{role}</button>)}</div>
    <p className="mb-2 text-[10px] text-slate-500">{'外框：查看角色 · ●：目前執行'}</p>
    {formalMode && <p data-testid="formal-agent-source" className="mb-2 text-[10px] text-slate-500">正式操作控制狀態；工具呼叫與原生效果須另有證據。</p>}
    <div className="rounded-lg border border-primary-100 bg-primary-50/40 p-3"><div className="flex justify-between gap-2"><b className="text-sm text-primary-950">{agent?.role ?? '等待 Agent'} · {agent?.phase ?? '—'}</b><span className="text-xs text-slate-600">{agent?.status ?? '—'}{elapsed !== null && ['started', 'running', 'waiting'].includes(agent?.status ?? '') ? ` · ${elapsed.toFixed(1)}s` : ''}</span></div><p className="mt-1 break-all text-xs">{'工具'}: <code>{agent?.tool ?? '—'}</code> · {'處理輪次'}: {agent?.cycle != null ? agent.cycle + 1 : '—'}</p></div>
    {stale && <p className="mt-2 text-[10px] text-amber-800">{'狀態待更新；顯示最後已知紀錄。'}</p>}
    <p className="mt-2 text-[10px] text-slate-600" data-testid="agent-target">{'目標'}: {!formalMode && observed?.targetRefs?.length ? [...new Set(observed.targetRefs.map((r) => `${r.mapNodeId}${r.slice ? ` / ${r.slice}` : ''}${r.shared ? '（共享）' : ''}`))].join(' · ') : '未提供可核對的目標'}</p>
  </section>;
}
function DecisionSummary({ adapter }: {
    adapter: ScenarioAdapter;
}) {
    const decisions = adapter.observation?.data?.decisions ?? [];
    const decision = decisions.find((d) => d.id === adapter.selectedDecisionId) ?? decisions.slice(-1)[0];
    const selectEvidence = (ref: string) => { const sample = adapter.observation?.samples.find((s) => s.id === ref); if (sample) {
        adapter.selectScenario?.(sample.scenario);
        adapter.selectSample?.(sample.id);
        adapter.selectDecision?.(decision?.id ?? null);
    } };
    return <section data-testid="network-ai-decision"><EvidenceDisclosure title={'決策依據與量測參照'}>{decision ? <><p className="text-xs font-semibold text-primary-800">{decision.action === 'hold' ? '維持現況 · 無寫入' : decision.action} · {'處理輪次'} {decision.cycle + 1} · {decision.outcome ?? '—'}</p><p className="mt-2 whitespace-pre-wrap text-xs leading-relaxed text-slate-700">{decision.reason || '—'}</p><div className="mt-2 flex flex-wrap gap-2">{[...(decision.beforeSampleIds ?? []), ...(decision.afterSampleIds ?? [])].map((ref) => <button key={ref} type="button" onClick={() => selectEvidence(ref)} className="max-w-full truncate rounded border border-primary-100 bg-primary-50 px-2 py-1 text-[10px] text-primary-800" title={ref}>{'證據'}: {ref}</button>)}</div></> : <p className="text-xs text-slate-500">{'等待可查證的決策依據。'}</p>}</EvidenceDisclosure></section>;
}
export function selectedRoundChanges(adapter: ScenarioAdapter): RoundChange[] {
    const scenario = adapter.selectedScenario ?? 'web_browsing';
    const changes = adapter.observation?.data?.changes ?? [];
    return changes.filter((change) => {
        if (!/policy|qer/i.test(change.kind) || change.scenario !== scenario)
            return false;
        if (adapter.selectedDecisionId)
            return change.decisionId === adapter.selectedDecisionId;
        if (adapter.selectedSampleId)
            return Boolean(change.beforeSampleIds?.includes(adapter.selectedSampleId) || change.afterSampleIds?.includes(adapter.selectedSampleId));
        return true;
    }).slice(-5);
}
function QerEvidence({ evidence, label }: {
    evidence: (Pick<QerEvidenceSummary, 'uplinkKbps' | 'downlinkKbps' | 'ueCount'> & Partial<QerEvidenceSummary>) | null | undefined;
    label?: string;
}) {
    if (!evidence)
        return <span>—</span>;
    const rate = (range: QerEvidenceSummary['uplinkKbps']) => !range ? '—' : range.min === range.max ? value(range.min) : `${value(range.min)}–${value(range.max)}`;
    return <span className="block"><b>{evidence.profileId ?? '—'}</b><span className="mt-1 block text-[9px] text-slate-600">UL {rate(evidence.uplinkKbps)} / DL {rate(evidence.downlinkKbps)} kbps</span><span className="block text-[9px] text-slate-500">{evidence.ueCount ?? '—'} UE · {label ?? (evidence.state === 'installed' ? '安裝憑證' : '原始 QER 觀測')}</span></span>;
}
export function ResourceEvidence({ resources, adapter }: {
    resources: ActionResource[];
    adapter: ScenarioAdapter;
}) {
    if (!resources.length)
        return <p>{'未保存此動作的 Pod／CPU／memory 證據。'}</p>;
    return <div className="space-y-3"><p className="text-[10px] text-slate-500">{'範圍：工具實際觀測的 NF／Pod；不是所選切片的專屬資源統計。'}</p>{resources.map((resource, i) => <article key={i} className="rounded border border-slate-200 p-2">
    <b>{resource.tool ?? resource.source ?? '—'} · {resource.status}</b><p className="mt-1 text-[10px] text-slate-500">{'本次決策觀測；不代表安裝後 readback'} · {resource.observedAt ?? '—'}</p>
    {resource.workloads.map((w, j) => <div key={j} className="mt-2 rounded bg-slate-50 p-2" data-testid="action-pod-status"><b>{w.nfType ?? '—'} · {w.workloadRef ?? '—'}</b><p>Pod UID: {w.podUid ?? '—'} · {w.sharing === 'shared' ? '共享 NF／Pod' : '歸屬未知'}</p><p>{'已觀測 Pod Ready'}: {w.ready == null ? '—' : w.ready ? '是' : '否'} · {'控制器 Ready／期望副本'}: {w.replicaScope === 'controller' ? w.ownerReadyPods ?? '—' : '—'} / {w.replicaScope === 'controller' ? w.desiredPods ?? '—' : '—'} · {w.phase ?? '—'} · {'重啟'}: {w.restarts ?? '—'}</p></div>)}
    {resource.bindings?.map((binding, j) => <p key={`binding-${j}`} className="mt-2 text-[10px] text-slate-500">{binding.slice} → {binding.podUid ?? '—'} · {binding.status} · {binding.sharing}</p>)}
    {resource.metrics.map((m, j) => {
                const fresh = freshTimestamp(m.observedAt, adapter.now);
                const usable = ['available', 'partial'].includes(resource.status) && ['uid_matched', 'creation_time_fenced'].includes(m.identityStatus) && Boolean(m.podUid) && m.status === 'observed' && (Boolean(adapter.selectedSampleId) || fresh);
                return <div key={j} className="mt-2 rounded bg-slate-50 p-2" data-testid="action-resource-metric" data-metric-fresh={String(fresh)}><b>{m.nfType ?? '—'} · {m.workloadRef ?? '—'}</b><p className="text-[10px] text-slate-500">Pod UID: {m.podUid ?? '—'} · {m.observedAt ?? '—'} · {m.sharing === 'shared' ? '共享 NF／Pod' : '歸屬未知，非切片專屬量測'}</p>
        {!fresh && <p className="text-amber-800">{adapter.selectedSampleId ? '歷史資源觀測' : '資源量測已過期'}</p>}
        {m.containers.map((c, k) => <p key={k}>{c.name ?? '—'} · CPU {value(usable && c.cpuCores !== null ? c.cpuCores * 1000 : null, ' mCPU')} · memory {value(usable && c.memoryBytes !== null ? c.memoryBytes / 1048576 : null, ' MiB')}</p>)}</div>;
            })}
  </article>)}</div>;
}
export function ActionDetails({ adapter }: {
    adapter: ScenarioAdapter;
}) {
    const data = adapter.observation?.data;
    const all = roundActions(data, adapter.observation?.events ?? []);
    const actions = scopedActions(all, data, adapter.selectedScenario ?? 'web_browsing', adapter.selectedSampleId, adapter.selectedDecisionId, adapter.selectedAgentRole);
    const selected = selectedActivityAction(actions, adapter.selectedActionId, data?.agent?.callId);
    return <section className={card} data-testid="action-details"><div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-sm font-bold">{'AI 操作流程'}</h3><label className="text-[10px] text-slate-600">{'查看 Agent'} <select data-testid="agent-role-selector" aria-label={'依 Agent 篩選'} value={adapter.selectedAgentRole ?? ''} className="min-h-9 rounded border border-slate-200 bg-white px-2" onChange={(e) => adapter.selectAgentRole?.(e.target.value ? e.target.value as AgentRole : null)}><option value="">{'全部 Agent'}</option>{(['planner', 'validator', 'executor'] as AgentRole[]).map((role) => <option key={role} value={role}>{role}</option>)}</select></label></div>
    <select data-testid="action-selector" aria-label={'選擇動作'} value={selected?.id ?? ''} className="mt-2 min-h-10 w-full rounded border border-slate-200 bg-white px-2 text-xs" onChange={(event) => {
            const action = actions.find((a) => a.id === event.target.value);
            if (!action)
                return;
            if (action.scenario && action.scenario !== adapter.selectedScenario)
                adapter.selectScenario?.(action.scenario as ScenarioGroup);
            const decision = data?.decisions.find((d) => d.id === action.decisionId);
            const sample = adapter.observation?.samples.find((s) => decision?.afterSampleIds?.includes(s.id) && s.scenario === (action.scenario ?? adapter.selectedScenario));
            if (sample)
                adapter.selectSample?.(sample.id);
            if (decision)
                adapter.selectDecision?.(decision.id);
            adapter.selectAction?.(action.id);
        }}>{(!actions.length || !selected) && <option value="">{'尚無符合篩選的動作'}</option>}{actions.map((a) => <option key={a.id} value={a.id}>{a.cycle === null ? '—' : a.cycle + 1} · {a.role ?? '—'} · {a.action ?? a.tool ?? a.target ?? '未知動作'}</option>)}</select>
    {selected && <SelectedActionDetails key={`${selected.id}:${adapter.selectedAgentRole ?? ''}`} adapter={adapter} action={selected}/>}
  </section>;
}
export function ActivitySummary({ adapter, action }: {
    adapter: ScenarioAdapter;
    action: ActionSummary;
}) {
    const event = (adapter.observation?.events ?? []).filter((e) => (action.callId ? e.callId === action.callId : e.cycle === action.cycle)
        && (!adapter.selectedAgentRole || e.role === adapter.selectedAgentRole)).slice(-1)[0];
    const decision = adapter.observation?.data?.decisions.find((d) => d.id === action.decisionId);
    const actionLabel = action.action === 'hold' ? '維持現況 · 無寫入'
        : action.action === 'apply' ? '套用網路政策'
            : action.action === 'restore' ? '還原網路政策' : action.tool ?? action.action ?? '—';
    const targets = [...new Set(action.targetRefs?.map((r) => `${r.mapNodeId}${r.slice ? ` / ${r.slice}` : ''}${r.shared ? '（共享）' : ''}`))];
    return <div className="mt-3 rounded-lg border border-primary-100 bg-primary-50/40 p-3" data-testid="action-summary" data-action-id={action.id}>
    <p className="text-xs font-semibold text-primary-950">{action.role ?? event?.role ?? '—'} · {actionLabel}</p>
    <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-2 text-[10px]">{[
            ['工具', action.tool ?? event?.tool ?? '—'], ['目標', targets?.length ? targets.join(' · ') : action.target && action.target !== action.tool ? action.target : '未知'],
            ['結果', action.type === 'decision' ? decision?.outcome ?? '—' : event?.status ?? '—'],
            ['當時時間', event?.at ?? action.at ?? '—'],
        ].map(([label, result]) => <div key={String(label)}><dt className="text-slate-500">{label}</dt><dd className="mt-0.5 break-words text-slate-800">{result}</dd></div>)}</dl>
  </div>;
}
function SelectedActionDetails({ adapter, action }: {
    adapter: ScenarioAdapter;
    action: ActionSummary;
}) {
    const [openPanels, setOpenPanels] = useState<string[]>([]);
    const [rawOpen, setRawOpen] = useState(false);
    const role = adapter.selectedAgentRole ?? null;
    const revision = (adapter.observation?.events ?? []).filter((e) => action.callId ? e.callId === action.callId : e.cycle === action.cycle).slice(-1)[0]?.sequence ?? 0;
    const detail = useActionEvidence(adapter.snapshot?.roundId ?? adapter.observation?.data?.roundId ?? null, adapter.snapshot?.generation ?? adapter.observation?.data?.generation ?? null, action, Boolean(openPanels.length), revision, role, openPanels.includes('logs'));
    const raw = useActionLogs(adapter.snapshot?.roundId ?? adapter.observation?.data?.roundId ?? null, adapter.snapshot?.generation ?? adapter.observation?.data?.generation ?? null, action, rawOpen, role, revision);
    const panels = actionRelatedPanels(action, detail.evidence);
    const toggle = (key: string, open: boolean) => setOpenPanels((current) => open ? current.includes(key) ? current : [...current, key] : current.filter((s) => s !== key));
    const change = selectedRoundChanges(adapter).find((c) => c.id === action.actionId);
    const qer = detail.evidence?.qer;
    return <div className="mt-2 space-y-2">
    <ActivitySummary adapter={adapter} action={action}/>
    <p className="text-[10px] text-slate-500">{action.callId ? '範圍：此工具呼叫' : '範圍：同一處理輪次，包含多個工具操作'} · {'處理輪次'} {action.cycle === null ? '—' : action.cycle + 1}</p>
    {action.action === 'hold' && <p className="text-xs text-slate-600">{'維持現況：沒有新增政策或 Pod 變更。'}</p>}
    {!panels.qer && !panels.resources && action.action !== 'hold' && <p className="text-xs text-slate-500">{'此動作尚無已分類的資源證據；可展開紀錄檢視可用性。'}</p>}
    {openPanels.length > 0 && <p role={detail.error ? 'alert' : 'status'} data-testid="action-detail-status" className="text-xs text-amber-800">{detail.error || (detail.loading ? '讀取已保存證據…' : '')}</p>}
    {panels.qer && <div data-testid="action-qer-details"><EvidenceDisclosure title={'QER readback 與安裝／還原'} onOpen={(open) => toggle('qer', open)}>
      <p>{'請求設定'}: {qer?.requested?.profileId ?? change?.profileId ?? '—'} · UL {value(qer?.requested?.uplinkKbps)} / DL {value(qer?.requested?.downlinkKbps)} kbps</p>
      <div className="flex gap-3" data-testid="qer-before-after"><QerEvidence evidence={qer?.before ?? change?.before}/><span>→</span><QerEvidence evidence={qer?.readback ?? null} label={'原生 QER readback'}/></div>
      <p data-testid="qer-readback-status" data-consistency={qer?.consistency ?? 'unknown'} data-status={qer?.status ?? 'missing'}>{'原生驗證'}: {qer?.consistency ?? 'unknown'} · {qer?.status ?? '待確認'} · {qer?.observedAt ?? '—'}</p>
      {qer?.reason && <p className="text-amber-800">{qer.reason}</p>}
      <p>{'安裝憑證'}: {change?.installed === true ? '已證實' : '待確認'} · {'效果驗證'}: {change?.effectVerified === true ? '已證實' : '待確認'}</p>
      {change?.restorationVerified === true && <p className="text-emerald-700">{'清理證據：已還原'} · {change.restoredAt ?? '—'}</p>}
    </EvidenceDisclosure></div>}
    {panels.resources && <div data-testid="action-resource-details"><EvidenceDisclosure title={'目標 Pod／CPU／memory 觀測'} onOpen={(open) => toggle('resources', open)}><ResourceEvidence resources={detail.evidence?.resources ?? []} adapter={adapter}/></EvidenceDisclosure></div>}
    <div data-testid="action-log-details"><EvidenceDisclosure title={'工具執行摘要與診斷分類'} onOpen={(open) => toggle('logs', open)}>
      <p className="text-[10px] text-slate-500">{action.callId ? '此工具呼叫' : '本次決策觀測（同 cycle）'}</p>
      <p className="break-all text-[10px] text-slate-500">cycle {action.cycle ?? '—'} · call {action.callId ?? '—'}</p>
      <p className="text-[10px] text-slate-500">{'以下是結構化執行摘要與分類，不是原始 log。'}</p>
      {detail.events.map((event) => <article key={event.sequence} className="rounded border border-slate-100 p-2"><b>#{event.sequence} · {event.role ?? '—'} · {event.tool ?? event.phase ?? '—'} · {event.status ?? '—'}</b><p>{event.at ?? '—'} · {event.durationMs ?? '—'} ms</p><EvidenceDisclosure title={'輸入／輸出摘要'}><pre className="whitespace-pre-wrap break-all text-[10px]">{JSON.stringify({ input: event.inputSummary, output: event.outputSummary }, null, 2)}</pre></EvidenceDisclosure></article>)}
      {!detail.events.length && !detail.loading && <p>{'尚無可關聯的紀錄。'}</p>}
      <EvidenceDisclosure title={'診斷分類資料'}>{detail.evidence?.diagnostics.map((diagnostic, i) => <pre key={i} className="whitespace-pre-wrap break-all text-[10px]">{JSON.stringify(diagnostic, null, 2)}</pre>)}</EvidenceDisclosure>
      {detail.gap && <p className="text-amber-800">{'紀錄有缺口或已達顯示上限。'}</p>}
      {detail.hasMore && <button data-testid="action-log-load-more" type="button" disabled={detail.loading} onClick={detail.loadMore} className="min-h-9 rounded border border-slate-200 px-3">{'載入更多'}</button>}
    </EvidenceDisclosure></div>
    <div data-testid="action-raw-log-details"><EvidenceDisclosure title={'當時保存的原始 log'} onOpen={setRawOpen}>
      <p className="text-[10px] text-slate-500">{'僅顯示與所選 Agent／動作有關聯、已保存且遮蔽敏感內容的原文。'}</p>
      <p data-testid="action-raw-log-status" role={raw.error ? 'alert' : 'status'} className="mt-2 text-xs text-amber-800">{raw.error || (raw.loading ? '讀取保存的原始 log…' : raw.status === 'not_captured' ? '此動作未保存原始 log。' : raw.status === 'unavailable' ? '此動作的原始 log 無法取得。' : raw.status === 'captured' ? '當時保存的紀錄' : '')}</p>
      {raw.reason && <p className="text-[10px] text-slate-500">{raw.reason}</p>}
      {raw.sources.map((source) => <p key={source.sourceRef} className="break-all text-[10px] text-slate-500">{source.workloadRef ?? '—'} · Pod {source.podUid ?? '—'} · {source.container ?? '—'} · {source.observedAt ?? '—'}</p>)}
      {raw.lines.map((line) => <pre key={line.sequence} className="whitespace-pre-wrap break-all rounded bg-slate-950 p-2 text-[10px] text-slate-100">{line.at ?? '—'} · {line.text}</pre>)}
      {raw.gap && <p className="text-amber-800">{'原始 log 有缺口或達顯示上限。'}</p>}
      {raw.hasMore && <button type="button" data-testid="action-raw-log-load-more" disabled={raw.loading} onClick={raw.loadMore} className="min-h-9 rounded border border-slate-200 px-3">{'載入更多原始 log'}</button>}
    </EvidenceDisclosure></div>
  </div>;
}
function Records({ adapter }: {
    adapter: ScenarioAdapter;
}) {
    return <div className="space-y-2"><EvidenceDisclosure title={'操作稽核與原始資料'}><OnpremPolicyPanel /><pre className="whitespace-pre-wrap break-all text-[10px]">{JSON.stringify(adapter.observation?.samples ?? [], null, 2)}</pre></EvidenceDisclosure><EvidenceDisclosure title={'執行限制、資料品質與日常觀測'}><OnpremRoundEvidence adapter={adapter}/><DailyNativeObservationPanel placement="inline"/></EvidenceDisclosure></div>;
}
export function NetworkAiPanel({ outcomes = false }: {
    outcomes?: boolean;
}) {
    const adapter = useOnpremScenario()!;
    const scenario = adapter.selectedScenario ?? 'web_browsing';
    const latestHistory = adapter.observation?.samples.filter((s) => s.scenario === scenario).slice(-1)[0]?.id;
    useEffect(() => { if (!outcomes)
        adapter.selectSample?.(null); }, [outcomes]);
    useEffect(() => {
        if (outcomes && !adapter.selectedSampleId && latestHistory)
            adapter.selectSample?.(latestHistory);
    }, [outcomes, scenario, latestHistory, adapter.selectedSampleId]);
    return <div className="space-y-3" data-testid={outcomes ? 'network-ai-outcomes' : 'network-ai-live'}><RoundStatus adapter={adapter}/>
    {adapter.observation?.error && <p role="alert" className="rounded-lg bg-amber-50 p-2 text-xs text-amber-900">{'觀測連線中斷，保留最後紀錄：'}{adapter.observation.error}</p>}
    {(adapter.observation?.gap || adapter.observation?.truncated) && <p role="status" className="rounded-lg bg-amber-50 p-2 text-xs text-amber-900">{'歷史紀錄有缺口或已達顯示上限，曲線與紀錄可能不完整。'}</p>}
    {outcomes ? <QosTimeline adapter={adapter}/> : <AgentStatus adapter={adapter}/>}
    <ActionDetails adapter={adapter}/><DecisionSummary adapter={adapter}/><Records adapter={adapter}/>
    <div className="sticky bottom-0 flex justify-end border-t border-slate-100 bg-white/95 py-2"><RoundCleanupControl adapter={adapter}/></div>
  </div>;
}
