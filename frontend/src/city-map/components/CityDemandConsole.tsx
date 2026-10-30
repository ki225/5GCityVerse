import { useEffect, useRef, useState } from 'react';
import { useOnpremScenario } from '../../onprem/components/OnpremRuntime/ScenarioContext';
import { OnpremRoundEvidence, ScenarioLifecyclePanel } from '../../onprem/components/OnpremRuntime/ScenarioPanels';
import { admissionLimit, allocationAdmitted, roundIsReleased } from '../../onprem/services/onpremScenarios';
import { previewServiceRequest, type ServicePreview } from '../../onprem/services/serviceRequirements';
import { useCityDemand } from '../context/CityDemandContext';
import { demandNames } from '../domain/demand';
import { useFormalIntent } from '../context/FormalIntentContext';
import { useNativeExecutionReady } from '../../onprem/components/OnpremRuntime/CityBootstrapGate';
export function CityDemandConsole({ phase, onNext }: {
    phase: 'review' | 'launch';
    onNext: () => void;
}) {
    const city = useCityDemand()!, adapter = useOnpremScenario()!;
    const formal = useFormalIntent()!;
    const executionReady = useNativeExecutionReady();
    const [intentText, setIntentText] = useState('');
    const request = city.compiled.request, fingerprint = JSON.stringify(request);
    const controller = useRef<AbortController | null>(null);
    const [preview, setPreview] = useState<{
        key: string;
        value: ServicePreview;
        at: number;
    } | null>(null);
    const [previewing, setPreviewing] = useState(false), [error, setError] = useState('');
    useEffect(() => {
        controller.current?.abort();
        setPreview(null);
        setPreviewing(false);
        setError('');
        return () => controller.current?.abort();
    }, [fingerprint]);
    const total = city.rows.reduce((sum, row) => sum + row.ueCount, 0);
    const allocation = request ? { baselineUes: request.baselineUes, events: request.events, total } : null;
    const acceptsNext = adapter.snapshot?.lifecycle?.canSubmitEvaluation === true;
    const locked = formal.pending || formal.uncertain || Boolean(adapter.snapshot && !roundIsReleased(adapter.snapshot) && !acceptsNext);
    const admitted = allocationAdmitted(adapter.snapshot?.capabilities, allocation);
    const available = formal.caps?.write_configured === true && formal.caps.planner_available === true && formal.caps.observation_available === true;
    const validPreview = preview?.key === fingerprint && preview.value.admissible && Date.now() - preview.at < 60000;
    const canSubmit = Boolean(request && intentText.trim() && formal.controlToken.trim() && validPreview && admitted && available
        && executionReady && adapter.snapshot?.capabilities.start === true && !locked && !(adapter.blockingError ?? Boolean(adapter.error)));
    async function inspect() {
        if (!request || previewing)
            return;
        controller.current?.abort();
        const abort = new AbortController();
        controller.current = abort;
        const timeout = window.setTimeout(() => {
            abort.abort();
            setPreviewing(false);
            setError('接入預覽逾時，請確認後端狀態後重試。');
        }, 30000);
        setPreviewing(true);
        setPreview(null);
        setError('');
        try {
            const result = await previewServiceRequest({ ...request, requestId: crypto.randomUUID() }, abort.signal);
            if (abort.signal.aborted)
                return;
            const resolution = result.spatialResolution, catalog = city.caps?.cityAccessCatalog;
            const groupIds = request.demandGroups!.map(row => row.groupId);
            if (!resolution || resolution.topologyRevision !== request.topologyRevision || resolution.spatialRevision !== catalog?.spatialRevision
                || !Array.isArray(resolution.resolvedGroups) || resolution.resolvedGroups.length !== groupIds.length
                || new Set(resolution.resolvedGroups.map(row => row.groupId)).size !== groupIds.length
                || !resolution.resolvedGroups.every(row => groupIds.includes(row.groupId) && typeof row.gnbId === 'string'
                    && typeof row.endpoint === 'string' && Number.isFinite(row.distanceKm) && row.distanceKm >= 0)) {
                throw new Error('城市配置已變更或預覽缺少接入結果，請更新配置並重新預覽。');
            }
            setPreview({ key: fingerprint, value: result, at: Date.now() });
        }
        catch (cause) {
            if (!abort.signal.aborted)
                setError(String((cause as Error).message));
        }
        finally {
            window.clearTimeout(timeout);
            if (!abort.signal.aborted)
                setPreviewing(false);
        }
    }
    return <div className="space-y-4" data-testid="city-demand-console">
    {formal.submission?.traffic_request_id === adapter.snapshot?.requestId && formal.submission && <ScenarioLifecyclePanel adapter={adapter}/>}
    <section className="rounded-xl border border-emerald-200 bg-white p-4">
      <h3 className="font-bold">{'城市需求草稿'} · {total} UE</h3>

      <p className="mt-2 text-xs">{'目前准入上限'}: {admissionLimit(adapter.snapshot?.capabilities) ?? '—'} UE</p>
      {!city.rows.length && <p className="mt-3 rounded bg-emerald-50 p-3 text-sm">{'尚無需求。請點選地圖上的建築，設定 UE 數量與網路需求。'}</p>}
      <ul className="mt-3 space-y-2">{city.rows.map(row => <li key={row.groupId} data-testid="city-demand-row" className="rounded-lg border border-slate-200 p-3 text-xs">
        <div className="flex items-center justify-between gap-2"><button type="button" className="min-h-9 text-left font-bold text-emerald-800 underline" onClick={() => city.focusDemand(row.groupId)}>{row.label} · {demandNames[row.profileId]}</button><b>{row.ueCount} UE</b></div>
        <p>({row.sourceLocation.position.x.toFixed(2)}, {row.sourceLocation.position.y.toFixed(2)}) km · UL / DL {city.services[row.profileId]?.traffic.offeredUplinkMbpsPerUe} / {city.services[row.profileId]?.traffic.offeredDownlinkMbpsPerUe} Mbps / UE</p>
        <button type="button" className="mt-1 min-h-8 text-red-700" onClick={() => city.remove(row.groupId)}>{'移除需求'}</button>
      </li>)}</ul>
      {city.rows.length > 0 && <details className="mt-3 text-xs text-slate-500"><summary className="cursor-pointer">需求計算方式</summary><p className="mt-2">同類需求共用流量與 SLA，UE 依不重複的需求群組加總。草稿會在下一輪統一啟動，不會插入執行中的輪次。</p></details>}
      {city.rows.length > 0 && city.compiled.error && <p role="status" className="mt-2 text-xs text-amber-800">{city.compiled.error}</p>}
      {total > (admissionLimit(adapter.snapshot?.capabilities) ?? 0) && <p role="alert" className="mt-2 text-xs text-red-700">{'需求超過目前可接納人數，請減少 UE。'}</p>}
      {city.catalogError && <p role="alert" className="mt-2 text-xs text-amber-800">{city.catalogError}</p>}
      <details className="mt-3 text-xs text-slate-500"><summary className="cursor-pointer">服務設定</summary><button type="button" className="mt-2 min-h-9 rounded border px-3" onClick={city.refreshCatalog}>更新城市服務設定</button></details>
    </section>
    <section className="space-y-3 border-t border-slate-200 pt-3" aria-label="需求預覽">

      <button type="button" data-testid="city-demand-preview" disabled={!executionReady || !request || !admitted || locked || previewing} onClick={() => void inspect()} className="min-h-11 w-full rounded-lg border border-emerald-300 bg-emerald-50 px-3 disabled:opacity-50">{previewing ? '預覽中…' : '預覽接入與需求'}</button>
      {!executionReady && <p className="text-xs text-amber-800">網路尚未就緒，接入預覽與實驗啟動暫停；需求草稿可以繼續編輯。</p>}
      {preview?.key === fingerprint && <div data-testid="city-demand-preview-result" className="space-y-2 text-xs">
        <p className="font-bold">{validPreview ? '預覽可接受，60 秒內有效。' : '預覽已過期或不可接受，請重新預覽。'}</p>
        {preview.value.spatialResolution?.resolvedGroups.map(row => <p key={row.groupId}>{city.rows.find(item => item.groupId === row.groupId)?.label} → {row.gnbId} / {row.endpoint} · {row.distanceKm.toFixed(2)} km</p>)}
        <p>{'以上為預計接入；尚未啟動 UE，也未驗證網路容量或實際連通。'}</p>
        {preview.value.warnings.map((warning, index) => <p key={index} className="text-amber-800">{warning.scenario ?? ''} · {warning.code}</p>)}
      </div>}
      {error && <p role="alert" className="text-xs text-red-700">{error}</p>}
    </section>
    {phase === 'review' ? <button type="button" disabled={!request} onClick={onNext} className="min-h-11 w-full rounded-xl bg-emerald-700 px-4 py-3 font-bold text-white disabled:opacity-50">{'下一步：意圖與啟動'}</button>
            : <section className="space-y-3 rounded-xl border border-primary-200 bg-white p-4">
        <h3 className="font-bold">{'意圖與啟動'}</h3>
        <p className="text-sm font-bold">{'AI 動態策略'}</p>
        <p className="text-xs">{'QoS 模式：保留要求的原始發送量，不自動縮減。其他策略目前未開放。'}</p>
        <label htmlFor="city-service-intent" className="block text-sm font-semibold">{'服務意圖：目標、有效期間與限制'}</label>
        <textarea id="city-service-intent" maxLength={8000} rows={4} value={intentText} onChange={event => setIntentText(event.target.value)} className="w-full rounded-lg border border-slate-300 p-3 text-sm" placeholder={'描述服務目標、持續時間及允許的調整；地圖位置、UE 與 SLA 將一併送出。'}/>
        <details className="text-xs">
          <summary className="cursor-pointer">操作授權</summary>
          <label className="mt-2 block">操作授權權杖
            <input type="password" aria-label="操作授權權杖" autoComplete="off" spellCheck={false}
              value={formal.controlToken} onChange={event => formal.setControlToken(event.target.value)}
              className="mt-1 block w-full rounded border border-slate-300 p-2"/>
          </label>
          <p className="mt-1">僅保留在此頁記憶體；清除或重新載入後須重新輸入。</p>
          <button type="button" onClick={() => formal.setControlToken('')} className="mt-2 min-h-9 rounded border px-3">清除操作授權</button>
        </details>
        {!formal.controlToken.trim() && <p role="status" className="text-xs text-amber-800">請先展開「操作授權」並輸入權杖，才能開始實驗。</p>}
        <button type="button" onClick={formal.refresh} className="min-h-9 rounded border px-3 text-xs">{'更新正式 AI 能力'}</button>
        {!available && <p role="status" className="text-xs text-amber-800">{'正式 AI 尚不可執行：需要寫入授權、Planner 與觀測服務。'} {formal.capError}</p>}
        <button data-testid="scenario-submit" type="button" disabled={!canSubmit} onClick={() => { if (canSubmit && request && preview && Date.now() - preview.at < 60000)
                void formal.submit(intentText, { ...structuredClone(request), requestId: crypto.randomUUID() }); }} className="min-h-11 w-full rounded-xl bg-primary-700 px-4 py-3 font-bold text-white disabled:opacity-50">{formal.pending ? '提交中…' : '開始實驗'}</button>
        {locked && <p role="status" className="text-xs text-amber-800">{'目前輪次仍持有資源或提交結果待確認；草稿可編輯，暫不可啟動。'}</p>}
      </section>}
    {formal.error && <p role="alert" className="text-xs text-red-700">{formal.error}</p>}
    {formal.uncertain && <section className="rounded border border-amber-300 bg-amber-50 p-3 text-xs"><p>{'前次提交結果待確認。重新核對會沿用已保存的原始需求與同一個 Idempotency-Key，不會提交目前的新草稿。'}</p><button type="button" disabled={formal.pending} onClick={() => void formal.retry()} className="mt-2 min-h-10 rounded border px-3">{'核對原始提交'}</button></section>}
    {formal.submission && <section className="rounded border border-primary-200 bg-primary-50 p-3 text-xs"><b>{'正式需求已接受，尚不代表服務達標'}</b><p className="mt-1 break-all">{formal.submission.intent_id} · {formal.active?.latest_operation?.stage ?? formal.submission.status}</p><button type="button" onClick={() => formal.openWorkspace(formal.submission!.intent_id)} className="mt-2 min-h-10 rounded border px-3">{'查看需求、操作與釋放'}</button></section>}
    {formal.submission?.traffic_request_id === adapter.snapshot?.requestId && formal.submission && <OnpremRoundEvidence adapter={adapter}/>}
  </div>;
}
