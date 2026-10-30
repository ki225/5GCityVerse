import { admissionLimit, diagnosticFields, receiverEvidenceCoverage, roundIsReleased, validWorkload, validSessionWorkloadSummary, validCapacityPerformance, validSlaAssessment, verifiedReceiverMeasurements, type ScenarioSnapshot } from '../../services/onpremScenarios';
import { useOnpremScenario, type ScenarioAdapter } from './ScenarioContext';
import { ServiceSlaResults } from './ServiceSlaResults';
import { useFormalIntent } from '../../../city-map/context/FormalIntentContext';
export function ScenarioLifecyclePanel({ adapter }: {
    adapter: ScenarioAdapter;
}) {
    const state = adapter.snapshot?.lifecycle;
    if (!state || !adapter.snapshot?.requestId) return null;
    const labels: Record<string, string> = {
        idle: '閒置', preparing: '建立環境中', running: '運作中',
        stopping: '停止中', stopped: '已停止', pending: '等待評估',
        completed: '評估完成', failed: '失敗', cancelled: '已取消',
        reserved: '已預留', held: '持有中', releasing: '釋放中',
        released: '已釋放', unknown: '待確認', queued: '排隊中',
        dispatching: '開始處理', blocked: '受阻'
    };
    return <section data-testid="scenario-lifecycle" className="space-y-2 rounded-xl border border-primary-200 bg-primary-50 p-3 text-xs">
    {state && <><div className="grid grid-cols-3 gap-2"><p>{'情境'}<b className="block">{labels[state.scenarioState] ?? state.scenarioState}</b></p><p>{'AI 評估'}<b className="block">{labels[state.evaluationState] ?? state.evaluationState} · {state.cyclesCompleted}/{state.maximumCycles}</b></p><p>{'資源'}<b className="block">{labels[state.resourceState] ?? state.resourceState}</b></p></div>
      {state.scenarioState === 'preparing' && <p>{'環境建立期間無法提交評估。'}</p>}
      {state.scenarioState === 'running' && state.evaluationState === 'completed' && <p>{'設定持續運作，直到停止或下一筆情境接替。'}</p>}</>}

  </section>;
}
export function onpremJourneyStep(stage?: string): 1 | 2 | 3 | 4 {
    if (['results_ready', 'cleaning', 'completed'].includes(stage || ''))
        return 4;
    if (['applying_policy', 'verifying_policy', 'measuring_result'].includes(stage || ''))
        return 3;
    if (['preflight', 'allocating', 'registering', 'establishing_pdu', 'verifying_connectivity', 'measuring_baseline'].includes(stage || ''))
        return 3;
    return 1;
}
export function OnpremRoundOutcome({ snapshot: s, compact = false }: {
    snapshot: ScenarioSnapshot | null;
    compact?: boolean;
}) {
    const performance = s && validCapacityPerformance(s) ? s.performance : undefined;
    const sla = s?.sla && validSlaAssessment(s.sla) ? s.sla : undefined;
    const aborted = s?.executionOutcome === 'failed' || s?.executionOutcome === 'cancelled';
    const outcome = s?.executionOutcome === 'failed' ? '失敗'
        : s?.executionOutcome === 'cancelled' ? '已取消'
            : s?.executionOutcome === 'passed' ? '通過' : '尚無結果';
    const cleanup = s?.cleanupStatus === 'verified' ? '已驗證釋放'
        : s?.cleanupStatus === 'blocked' ? '受阻'
            : s?.cleanupStatus === 'pending' ? '待確認' : '尚未驗證';
    return <div role="status" data-testid="onprem-round-outcome" data-execution-outcome={s?.executionOutcome ?? ''} data-cleanup-status={s?.cleanupStatus ?? ''} className={`rounded-lg border px-3 py-2 text-sm font-bold ${aborted ? 'border-amber-300 bg-amber-50 text-amber-950' : 'border-slate-300 bg-slate-50 text-slate-900'}`}>
    <span>{'執行結果'}: {outcome}</span><span> · {'清理'}: {cleanup}</span>
    <p data-testid="onprem-capacity-verdict" data-performance-status={performance?.status ?? 'unavailable'} className={`mt-1 text-xs ${performance?.status === 'failed' ? 'text-red-800' : 'text-slate-700'}`}>
      {'流量品質（本輪歷史量測）'}: {performance?.status === 'passed'
            ? '達標' : performance?.status === 'failed' ? '未達標'
            : performance?.status === 'unavailable' && ['measurements_not_ready', 'round_not_completed'].includes(performance.reason)
                ? '等待完整量測' : '量測證據不可用'}
      {performance?.status === 'failed' && ` · ${performance.failedFlowCount} / ${performance.flowCount} ${'資料流未達標'}`}
    </p>
    <p data-testid="onprem-sla-verdict" data-sla-status={sla?.status ?? 'unknown'} className="mt-1 text-xs">
      {sla && 'schemaVersion' in sla ? '服務 SLA（最近完整評估視窗）' : '應用 SLA（最後結果視窗）'}: {sla?.status === 'met' ? '達標'
            : sla?.status === 'not_met' ? '未達標' : '未知：目標或量測不足'}
      {'；獨立於流程通過與政策相對改善，不代表持續達標。'}
    </p>
    {!compact && sla && 'schemaVersion' in sla && <ServiceSlaResults assessment={sla}/>}
    {!compact && <p className="mt-1 text-xs">{'流程通過不代表流量品質達標；此判定不代表目前網路狀態、容量認證或 QoS 政策效果。'}</p>}
    {aborted && <p className="mt-1 text-xs">{'清理完成不代表執行成功。'}</p>}
  </div>;
}
export function OnpremRoundEvidence({ adapter }: {
    adapter: ScenarioAdapter;
}) {
    const s = adapter.snapshot;
    const cap = s?.capabilities;
    const limit = admissionLimit(cap);
    const visibleMeasurements = adapter.fresh ? verifiedReceiverMeasurements(s, adapter.now) : [];
    const coverage = receiverEvidenceCoverage(s, adapter.fresh ? adapter.now : Number.POSITIVE_INFINITY);
    const trafficVisible = visibleMeasurements.some((row) => (row.receivedUplinkMbps ?? 0) > 0 || (row.receivedDownlinkMbps ?? 0) > 0);
    const running = s && !roundIsReleased(s) && !['failed', 'verification_unavailable', 'blocked', 'results_ready'].includes(s.stage);
    const progressLabels: Record<string, string> = { plan: '規劃調整', precheck: '檢查方案',
        apply: '套用政策', postcheck: '驗證網路效果',
        restore: '還原政策', restoration: '確認還原',
        completed: '迴圈已結束', failed: '執行失敗' };
    return <section className="space-y-3 rounded-xl border border-slate-200 bg-white p-4" aria-label="Actual on-prem round evidence" data-testid="onprem-round-evidence" data-round-id={s?.roundId ?? ''} data-sequence={s?.sequence ?? ''} data-generation={s?.generation ?? ''} data-policy-revision={s?.policyRevision ?? ''} data-stage={s?.stage ?? ''} data-fresh={String(adapter.fresh)} data-observed-at={s?.observedAt ?? ''} data-allocated={s?.counts.allocated ?? ''} data-registered={s?.counts.registered ?? ''} data-pdu-established={s?.counts.pduEstablished ?? ''} data-traffic-verified={s?.counts.trafficVerified ?? ''} data-visible-measurements={visibleMeasurements.length} data-traffic-visible={String(trafficVisible)}>
    <div role="status" className="flex items-center gap-2"><span aria-hidden="true" className={`h-2.5 w-2.5 rounded-full ${running && adapter.fresh ? 'motion-safe:animate-pulse bg-amber-500' : 'bg-slate-400'}`}/><h3 className="font-bold">{s?.stage ?? 'verification_unavailable'}</h3></div>
    <OnpremRoundOutcome snapshot={s}/>
    {s?.agentRuntime && <p data-testid="onprem-agent-progress" className="text-sm text-primary-900">
      {'AI 進度'}: {progressLabels[s.agentRuntime.phase] ?? '觀測網路'}
      {s.agentRuntime.cycle !== undefined && ` · ${'第'} ${s.agentRuntime.cycle + 1} ${'輪'}`}
    </p>}
    <p className="break-all text-xs text-slate-600">{s?.roundId ?? '—'} · generation {s?.generation ?? '—'} · policy {s?.policyRevision ?? '—'} · snapshot {s?.sequence ?? '—'}</p>
    <div data-testid="onprem-calibration-admission" data-verified-max-ues={cap?.verifiedMaxUes ?? ''} data-candidate-max-ues={cap?.candidateMaxUes ?? ''} data-admission-max-ues={limit ?? ''} data-commissioning-enabled={String(cap?.commissioningEnabled === true)} data-profile-id={cap?.commissioningProfile ?? ''} className="rounded bg-amber-50 p-2 text-xs">
      <b>{cap?.capacityStatus === 'unverified_ai_trial' ? '100 UE AI 實驗：容量未驗證' : cap?.commissioningEnabled === true ? '未驗證校準' : '校準未啟用'}</b>
      <p>{'已驗證情境流量上限'}: {cap?.verifiedMaxUes ?? '—'} · {'候選上限'}: {cap?.candidateMaxUes ?? '—'} · {'准入上限'}: {limit ?? '—'}</p>
    </div>
    {cap?.scopedVerifiedTrafficProfiles?.filter(profile => profile.validUntil === null || profile.validUntil > Date.now() / 1000).map(profile => <p key={profile.profileId} data-testid="onprem-scoped-capacity" className="text-xs">
      限定 web_browsing：已驗證 {profile.verifiedDevices}/100 UE · 每 UE 1 PDU · UL/DL 各 100 kbps
      {profile.validUntil !== null && ` · 有效至 ${new Date(profile.validUntil * 1000).toISOString()}`}
      {' · 僅限已驗證地理與資源配置；不是目前 UE 連線或政策寫入授權。'}
    </p>)}
    {!adapter.fresh && <p className="text-sm text-amber-900">{'沒有新鮮量測：不顯示流量動畫。'}</p>}
    {adapter.error && <p role="alert" className="break-words text-sm text-red-800">{adapter.error}</p>}
    {adapter.uncertain && <p role="alert" className="text-sm text-amber-900">{'提交結果未知，禁止重複送出；需後端核對原請求。'}</p>}
    <dl className="grid grid-cols-2 gap-2 text-sm">{(['allocated', 'registered', 'pduEstablished', 'trafficVerified'] as const).map((key) => <div key={key} className="rounded-lg bg-slate-50 p-2"><dt className="text-xs text-slate-600">{key === 'trafficVerified' ? '連線探測成功（非流量人數）' : key}</dt><dd data-testid={`onprem-count-${key}`} className="font-mono font-bold">{adapter.fresh ? s?.counts[key] ?? '—' : '—'}</dd></div>)}</dl>
    {s?.trafficCounts && <p data-testid="onprem-traffic-participation" className="text-xs">
      {'本輪要求／選定／最近接收端量測有流量 UE'}: {s.trafficCounts.requested} / {s.trafficCounts.selected} / {coverage.status === 'complete' ? s.trafficCounts.observedTransmitting ?? '—' : '—'}
      {coverage.status !== 'complete' ? '（接收端證據不完整／未知）' : s.trafficCounts.observedAt ? ` · ${s.trafficCounts.observedAt}` : '（尚無接收端量測）'}
    </p>}
    {coverage.expected > 0 && <div data-testid="onprem-receiver-coverage" data-status={coverage.status}
      data-expected={coverage.expected} data-session-bindings={coverage.sessionBindings} data-uplink={coverage.uplink}
      data-downlink={coverage.downlink} data-duplex={coverage.duplex} className="text-xs">
      <p>新鮮接收端證據：UL {coverage.uplink} / {coverage.expected} UE · DL {coverage.downlink} / {coverage.expected} UE · 雙向 {coverage.duplex} / {coverage.expected} UE</p>
      {coverage.status === 'unknown' && <p>證據不完整／未知：缺少 session 綁定 {coverage.missingSessionBindings}；UL {coverage.missingUplink}；DL {coverage.missingDownlink}。未量測不視為零流量或服務達標。</p>}
      <p>每個 UE 的單一 PDU session 不代表整個城市只有一個 UE；量測覆蓋不代表 SLA 達標。</p>
    </div>}
    <p className="text-xs">{'執行結果'}: {s?.executionOutcome ?? '—'} · {'清理'}: {s?.cleanupStatus ?? 'unknown'}</p>
    <p className="text-xs text-slate-600">{'結果可用不代表清理完成。此輪已知資源釋放不等於 SMF 全域零 session。'}</p>
    {s && s.errors.length > 0 && <section aria-label="Round diagnostic history" className="space-y-1">
      <h4 className="text-xs font-semibold text-slate-600">{'本輪診斷紀錄（非即時狀態）'}</h4>
      {s.errors.map((error, index) => <p key={index} className="break-words text-xs text-amber-900">{error}</p>)}
    </section>}
    <RoundCleanupControl adapter={adapter}/>
  </section>;
}
export function OnpremPolicyPanel() {
    const adapter = useOnpremScenario()!;
    const actions = adapter.snapshot?.actions ?? [];
    const policies = actions.filter((action) => action.kind === 'application_policy');
    const audits = actions.filter((action) => action.kind !== 'application_policy');
    const workload = audits.find((action) => action.kind === 'traffic_window'
        && (validWorkload(action.workload) || validSessionWorkloadSummary(action.workload)))?.workload;
    const diagnosticProbe = workload?.profileId === 'controlled_qer_probe_v1';
    const names: Record<string, string> = {
        ue_allocation: 'UE 配置',
        ue_stop: 'UE 停止／清理',
        traffic_window: '接收端量測視窗',
        no_additional_policy: '不新增策略（未送出 N5 政策）',
        zero_workload: '零 UE 工作負載',
    };
    const fields = ['requested', 'accepted', 'installed', 'effectVerified'] as const;
    const labels = {
        requested: '已請求', accepted: '已接受',
        installed: '已安裝', effectVerified: '效果已驗證',
    };
    const evidence = (action: typeof actions[number], policy: boolean) => <dl className="mt-2 grid grid-cols-2 gap-2 text-xs">{fields.map((key) => <div key={key}><dt>{policy ? labels[key] : `${'稽核欄位'}: ${key}`}</dt><dd>{action[key] === true ? '已記錄為是' : '未證實'}</dd></div>)}</dl>;
    const audit = (action: typeof actions[number]) => {
        const diagnostic = action.kind === 'execution_failure' || action.kind === 'traffic_failure';
        const detail = diagnosticFields(action);
        return <article key={action.id} data-testid="resource-action-audit" className={`mt-2 rounded-lg border p-3 ${diagnostic ? 'border-amber-300 bg-amber-50' : 'border-slate-200 bg-white'}`}>
      <b className="text-xs">{diagnostic ? (action.kind === 'execution_failure' ? '執行未完成診斷（非政策）' : '流量量測失敗診斷（非政策）') : names[action.kind ?? ''] ?? '未分類操作（不視為政策）'}</b>
      <p className="break-all text-xs text-slate-500">{action.id} · {action.kind ?? '類型未知'}</p>
      {diagnostic ? <dl data-testid="round-failure-diagnostic" className="mt-2 space-y-1 text-xs">
        <div><dt>{'診斷代碼'}</dt><dd>{detail.code}</dd></div>
        <div><dt>{'發生階段'}</dt><dd>{detail.stage}</dd></div>
        <div><dt>{'原始紀錄時間'}</dt><dd>{detail.observedAt ?? '—'}</dd></div>
      </dl> : evidence(action, false)}
    </article>;
    };
    return <section className="space-y-3" aria-label="Round action evidence">
    <h3 className="font-bold text-primary-950">{'本輪操作與量測紀錄'}</h3>
    <OnpremRoundOutcome snapshot={adapter.snapshot}/>
    <p className="text-sm text-amber-900">{adapter.snapshot?.capabilities.reason || '只顯示後端已證實的能力；OpenAI 可用不代表 QoS 操作可用。'}</p>
    <p className="text-xs text-slate-600">{'以下為此輪歷史稽核，不表示資源目前仍運作或政策仍安裝。即時狀態請見下方觀測。'}</p>
    {workload && <section data-testid="onprem-round-workload" data-profile-id={workload.profileId} data-workload-kind={workload.workloadKind} data-probe-kbps-per-direction={workload.probeKbpsPerDirection} data-comparison-fingerprint={workload.comparisonFingerprint} data-budget-kbps-per-direction={workload.budgetKbpsPerDirection} data-requested-uplink-kbps={workload.requestedUplinkKbps} data-requested-downlink-kbps={workload.requestedDownlinkKbps} data-requested-combined-kbps={workload.requestedCombinedKbps} data-applied-uplink-kbps={workload.appliedUplinkKbps} data-applied-downlink-kbps={workload.appliedDownlinkKbps} data-applied-combined-kbps={workload.appliedCombinedKbps} data-cap-reason={workload.capReason ?? ''} data-qos-effect-claimed="false" className="rounded border border-amber-200 p-3 text-xs">
      <h4 className="font-bold">{diagnosticProbe ? '受控 QER 診斷流量' : '固定送出負載（非 QoS 效果）'}</h4>
      <p>{workload.profileId}</p>
      {diagnosticProbe && <p>{'這是量測 QER 的診斷探測流量；不是應用正常流量，不能用來宣稱應用效能改善。'}</p>}
      <p>{'請求 UL / DL / 合計'}: {workload.requestedUplinkKbps} / {workload.requestedDownlinkKbps} / {workload.requestedCombinedKbps} kbps</p>
      <p>{'套用 UL / DL / 合計'}: {workload.appliedUplinkKbps} / {workload.appliedDownlinkKbps} / {workload.appliedCombinedKbps} kbps</p>
      <p>{workload.workloadKind === 'diagnostic_probe' ? '診斷探測：每方向預算' : workload.trafficMode === 'qos' ? 'QoS 實驗：未縮減負載；協定負載上界' : '功能測試：每方向流量預算'}: {workload.budgetKbpsPerDirection} kbps · {workload.capReason ?? '未封頂'}</p>
      <p>{'送出負載不是實測吞吐；不是 PCF／UPF 限速證明。'}</p>
      {validSessionWorkloadSummary(workload) ? <p>Session 工作負載彙總；此摘要未提供逐 UE 分配，不能據此推算 session 綁定或 QoS 效果。</p>
        : <table><thead><tr><th>SUPI</th><th>{'情境'}</th><th>{'請求 / 套用 kbps'}</th></tr></thead><tbody>{workload.perUe!.map((row) => <tr key={row.supi}><td>{row.supi}</td><td>{row.scenario}</td><td>{workload.serviceContractRevision ? `UL ${row.requestedUplinkKbps} / ${row.appliedUplinkKbps} · DL ${row.requestedDownlinkKbps} / ${row.appliedDownlinkKbps}` : `${row.requestedKbps} / ${row.appliedKbps}`}</td></tr>)}</tbody></table>}
    </section>}
    {policies.length > 0 ? <section aria-label="Official policy evidence"><h4 className="font-bold text-primary-950">{'官方 PCF → SMF → PFCP → UPF'}</h4>{policies.map((action) => <article key={action.id} data-testid="application-policy-audit" className="mt-2 rounded-lg border border-primary-200 bg-white p-3"><b className="text-xs">{'應用政策'} · {action.id}</b>{evidence(action, true)}</article>)}</section> : <p className="text-sm text-slate-600">{'此輪尚無應用政策操作紀錄；資源操作不代表 N5 政策已套用。'}</p>}
    {audits.length > 0 && <section aria-label="Resource and measurement audit"><h4 className="text-sm font-bold">{'資源與量測稽核（非政策確認）'}</h4>{audits.map(audit)}</section>}
    <OnpremRoundEvidence adapter={adapter}/>
  </section>;
}
export function RoundCleanupControl({ adapter }: {
    adapter: ScenarioAdapter;
}) {
    const formal = useFormalIntent();
    const s = adapter.snapshot;
    return <>{s?.roundId && !roundIsReleased(s) && formal?.submission && <button type="button" onClick={() => formal.openWorkspace(formal.submission!.intent_id)} className="min-h-11 rounded-lg border border-amber-400 px-4 text-sm font-bold text-amber-950">{'由正式需求管理停止與釋放'}</button>}</>;
}
