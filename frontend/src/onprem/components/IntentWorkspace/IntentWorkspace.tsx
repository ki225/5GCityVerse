import { useEffect, useRef, useState } from 'react';
import { IntentReport, OperationDetails } from './IntentReport';
import { intentEvidence, nextEvidenceDeadline } from './intentPresentation';
import { useFormalIntent } from '../../../city-map/context/FormalIntentContext';
import { MAX_INTENT_PAGE_OFFSET, getServiceIntent, getIntentMembers, getIntentOperationDetail, intentDetail, listServiceIntents, reconcileServiceIntent, releaseServiceIntent, type OperationDetailView, type ServiceIntentView, } from '../../../services/intents';
const labels: Record<string, string> = {
    compiling: '理解需求中', queued: '等待處理', observing: '觀測中', planning: '規劃中',
    validating: '檢查計畫', reserving: '保留資源', applying: '套用中',
    verifying: '驗證服務', completed: '操作完成',
    active: '管理中', satisfied: '服務已達標',
    degraded: '部分目標未達', infeasible: '無法滿足',
    needs_definition: '需要補充需求', expired: '已到期',
    compensated: '已補償', compensating: '補償中',
    recovery_required: '需要恢復處理', failed: '操作失敗',
    stale: '狀態已變更，需重新評估',
    waiting_session_reservation: '等待模擬器保留連線',
    starting_traffic: '啟動情境流量',
    waiting_traffic_admission: '等待情境啟動確認',
    waiting_native_session_binding: '等待網路建立連線',
    waiting_observation: '等待當前量測',
};
export function intentStateLabel(value: string): string {
    return labels[value] ?? value;
}
export function IntentWorkspace() {
    const dialog = useRef<HTMLDialogElement>(null);
    const [open, setOpen] = useState(false);
    const formal = useFormalIntent();
    const [items, setItems] = useState<ServiceIntentView[]>([]);
    const [selectedId, setSelectedId] = useState('');
    const [lookupId, setLookupId] = useState('');
    const [listOffset, setListOffset] = useState(0);
    const [memberOffset, setMemberOffset] = useState(0);
    const [memberPage, setMemberPage] = useState<{
        items: unknown[];
        total: number;
        rejected: unknown[];
    } | null>(null);
    const [selected, setSelected] = useState<ServiceIntentView | null>(null);
    const [evidenceNow, setEvidenceNow] = useState(() => Date.now() / 1000);
    const [operationId, setOperationId] = useState('');
    const [operationDetail, setOperationDetail] = useState<OperationDetailView | null>(null);
    const [operationError, setOperationError] = useState('');
    const [operationPending, setOperationPending] = useState(false);
    const operationRequest = useRef<AbortController | null>(null);
    const operationEpoch = useRef(0);
    const operationScope = useRef({ open, selectedId });
    operationScope.current = { open, selectedId };
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const queryKey = `${selectedId}\u0000${listOffset}\u0000${memberOffset}`;
    const currentQuery = useRef(queryKey);
    currentQuery.current = queryKey;
    useEffect(() => { if (formal?.workspace) {
        setSelectedId(formal.workspace.intentId);
        setSelected(null);
        setMemberPage(null);
        setOpen(true);
    } }, [formal?.workspace]);
    useEffect(() => {
        if (open && !dialog.current?.open)
            dialog.current?.showModal();
        if (!open && dialog.current?.open)
            dialog.current?.close();
    }, [open]);
    useEffect(() => {
        operationEpoch.current += 1;
        operationRequest.current?.abort();
        operationRequest.current = null;
        setOperationId('');
        setOperationDetail(null);
        setOperationError('');
        setOperationPending(false);
        return () => { operationEpoch.current += 1; operationRequest.current?.abort(); operationRequest.current = null; };
    }, [open, selectedId]);
    useEffect(() => {
        if (!open)
            return;
        let stopped = false;
        let timer: ReturnType<typeof setTimeout> | undefined;
        const update = () => {
            if (stopped)
                return;
            clearTimeout(timer);
            const now = Date.now() / 1000;
            setEvidenceNow(now);
            const deadline = nextEvidenceDeadline(selected, now);
            if (deadline !== null)
                timer = setTimeout(update, Math.max(1, Math.min(2147483647, Math.ceil((deadline - now) * 1000) + 1)));
        };
        const visibility = () => { if (document.visibilityState === 'visible')
            update(); };
        update();
        if (typeof document !== 'undefined')
            document.addEventListener('visibilitychange', visibility);
        return () => {
            stopped = true;
            clearTimeout(timer);
            if (typeof document !== 'undefined')
                document.removeEventListener('visibilitychange', visibility);
        };
    }, [open, selected]);
    useEffect(() => {
        if (!open)
            return;
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout> | undefined;
        const refresh = async () => {
            try {
                const listing = await listServiceIntents(controller.signal, listOffset);
                const detail = selectedId ? await getServiceIntent(selectedId, controller.signal) : null;
                const members = selectedId ? await getIntentMembers(selectedId, controller.signal, memberOffset) : null;
                if (!controller.signal.aborted && currentQuery.current === queryKey) {
                    setItems(listing.items);
                    setSelected(detail);
                    setMemberPage(members);
                    setError('');
                }
            }
            catch (failure) {
                if (!controller.signal.aborted && currentQuery.current === queryKey) {
                    setSelected(null);
                    setMemberPage(null);
                    setError(intentDetail(failure));
                }
            }
            finally {
                if (!controller.signal.aborted && currentQuery.current === queryKey)
                    timer = setTimeout(refresh, 5000);
            }
        };
        void refresh();
        return () => { controller.abort(); clearTimeout(timer); };
    }, [open, selectedId, listOffset, memberOffset, queryKey]);
    async function run(action: () => Promise<unknown>) {
        setBusy(true);
        setError('');
        try {
            await action();
        }
        catch (failure) {
            setError(intentDetail(failure));
        }
        finally {
            setBusy(false);
        }
    }
    async function readExactOperation() {
        if (!open || !selected)
            return;
        const id = operationId.trim().toLowerCase(), intentId = selected.intent_id;
        const epoch = ++operationEpoch.current;
        operationRequest.current?.abort();
        const controller = new AbortController();
        operationRequest.current = controller;
        setOperationDetail(null);
        setOperationError('');
        setOperationPending(true);
        const current = () => !controller.signal.aborted && operationEpoch.current === epoch
            && operationScope.current.open && operationScope.current.selectedId === intentId;
        try {
            const detail = await getIntentOperationDetail(id, intentId, controller.signal);
            if (current()) {
                if (detail.intent_id !== intentId || detail.operation_id !== id)
                    throw new Error('operation_not_in_selected_intent');
                setOperationDetail(detail);
            }
        }
        catch (failure) {
            if (current()) {
                const code = failure instanceof Error ? failure.message : '';
                setOperationError(/^(operation_[a-z0-9_]+|invalid_operation_lookup)$/.test(code) ? code : 'operation_response_unavailable');
            }
        }
        finally {
            if (current()) {
                setOperationPending(false);
                operationRequest.current = null;
            }
        }
    }
    const operation = selected?.latest_operation;
    const intentState = selected?.status ?? selected?.state ?? 'active';
    const currentSatisfied = selected ? intentEvidence(selected, evidenceNow).satisfied : false;
    const issues = selected?.definition_issues ?? [];
    return <>
    <dialog ref={dialog} onClose={() => setOpen(false)} aria-labelledby="intent-workspace-title" className="m-auto max-h-[90vh] w-[min(960px,94vw)] overflow-y-auto rounded-2xl border border-slate-600 bg-slate-950 p-6 text-slate-100 shadow-xl backdrop:bg-black/70">
      <div className="flex items-start justify-between gap-4">
        <div><h2 id="intent-workspace-title" className="text-xl font-bold">{'城市服務意圖'}</h2>
          <p className="mt-2 text-sm text-slate-300">{'描述服務對象、目標與有效期間。系統會檢查需求、可用資源，並分開回報配置與服務效果。'}</p></div>
        <button type="button" onClick={() => setOpen(false)} aria-label={'關閉服務意圖'} className="min-h-11 min-w-11 rounded border border-slate-500 px-3">×</button>
      </div>
      <p className="mt-5 text-sm text-slate-300">{'所有新需求均由城市地圖草稿與「意圖與啟動」統一提交。此處只查詢、重新評估或釋放既有需求。'}</p>
      {error && <p role="alert" className="mt-4 rounded border border-red-500 p-3 text-red-200">{error}</p>}
      <div className="mt-6 grid gap-5 md:grid-cols-[240px_1fr]">
        <nav aria-label={'服務意圖清單'}>
          <h3 className="font-semibold">{'需求清單'} ({listOffset + 1}–{listOffset + items.length})</h3>
          <form className="mt-3" onSubmit={(event) => { event.preventDefault(); if (!lookupId.trim())
        return; setSelected(null); setMemberPage(null); setMemberOffset(0); setSelectedId(lookupId.trim()); }}>
            <label htmlFor="intent-lookup-id" className="block text-xs">{'以需求 ID 唯讀查詢'}</label>
            <input id="intent-lookup-id" value={lookupId} onChange={event => setLookupId(event.target.value)} maxLength={128} className="mt-1 min-h-11 w-full rounded border border-slate-500 bg-slate-900 px-2"/>
            <button className="mt-2 min-h-11 rounded border border-slate-500 px-3" type="submit">{'查詢'}</button>
          </form>
          {items.length === 0 && <p className="mt-3 text-sm text-slate-400">{'尚無需求'}</p>}
          <ul className="mt-3 space-y-2">{items.map((item) => <li key={item.intent_id}><button type="button" onClick={() => { setSelected(null); setMemberPage(null); setMemberOffset(0); setSelectedId(item.intent_id); }} aria-current={selectedId === item.intent_id ? 'true' : undefined} className="w-full break-all rounded border border-slate-600 p-3 text-left text-sm hover:border-primary-400 aria-[current=true]:border-primary-300">
            <span className="block font-semibold">{item.source_event_id || item.intent_id}</span>
            <span className="mt-1 block text-slate-300">{(item.status ?? item.state) === 'satisfied' ? ('最近回報達標（開啟核對窗口）') : intentStateLabel(item.status ?? item.state ?? 'active')}</span>
          </button></li>)}</ul>
          <div className="mt-3 flex gap-2"><button type="button" disabled={listOffset === 0} onClick={() => setListOffset(Math.max(0, listOffset - 20))} className="min-h-11 rounded border px-2 disabled:opacity-40">{'上一頁'}</button><button type="button" disabled={items.length < 20 || listOffset >= MAX_INTENT_PAGE_OFFSET} onClick={() => setListOffset(listOffset + 20)} className="min-h-11 rounded border px-2 disabled:opacity-40">{'下一頁'}</button></div>
        </nav>
        <section aria-live="polite" aria-label={'需求進度'}>
          {!selected && <p className="text-slate-400">{selectedId ? ('讀取需求中…') : ('選擇需求以查看進度與結果。')}</p>}
          {selected && <>
            <h3 className="font-semibold">{'服務狀態'}：{intentState === 'satisfied' && !currentSatisfied ? ('目前達標證據未驗證') : intentStateLabel(intentState)}</h3>
            {intentState === 'satisfied' && !currentSatisfied && <p className="mt-1 text-xs text-slate-400">{'後端最近回報狀態'}：{intentStateLabel(intentState)}</p>}
            <p className="mt-2 text-sm text-slate-300">{'需求版本'}：{selected.revision}</p>
            {operation && <div className="mt-4 rounded-lg border border-slate-600 p-3">
              <p>{'最近操作'}：{intentStateLabel(operation.status)}</p>
              {operation.stage && <p className="mt-1 text-sm text-slate-300">{intentStateLabel(operation.stage)}</p>}
              {operation.error != null && <p className="mt-2 text-red-200">{intentDetail(operation.error)}</p>}
            </div>}
            {issues.length > 0 && <ul className="mt-3 list-inside list-disc text-amber-200">{issues.map((issue, i) => <li key={i}>{intentDetail(issue)}</li>)}</ul>}
            <p className="mt-4 text-sm text-slate-300">{'操作完成不等於服務已達標；服務狀態以有效量測與需求契約判定。'}</p>
            <div className="mt-4 flex flex-wrap gap-3">
              <button type="button" disabled={busy || intentState === 'expired'} onClick={() => void run(() => reconcileServiceIntent(selected.intent_id))} className="min-h-11 rounded border border-primary-400 px-4 disabled:opacity-40">{'重新評估'}</button>
              <button type="button" disabled={busy || intentState === 'expired'} onClick={() => void run(() => releaseServiceIntent(selected.intent_id))} className="min-h-11 rounded border border-amber-400 px-4 disabled:opacity-40">{'結束需求並回收'}</button>
            </div>
            <section className="mt-5 rounded border border-slate-600 p-3" aria-label={'精確操作唯讀查詢'}>
              <h4 className="font-semibold">{'操作詳情'}</h4>
              <p className="mt-2 text-xs text-slate-400">{'輸入此需求的操作 ID，只查詢該筆紀錄。較晚完成的回收不會取代原始失敗原因。'}</p>
              <form className="mt-3" onSubmit={event => { event.preventDefault(); void readExactOperation(); }}>
                <label htmlFor="intent-operation-id" className="block text-sm">{'操作 ID（UUID）'}</label>
                <input id="intent-operation-id" maxLength={128} required value={operationId} onChange={event => setOperationId(event.target.value)} autoComplete="off" className="mt-1 min-h-11 w-full rounded border border-slate-500 bg-slate-900 px-2"/>
                <button type="submit" disabled={!operationId.trim()} className="mt-2 min-h-11 rounded border border-slate-500 px-3">{operationPending ? ('重新查詢操作') : ('唯讀查詢操作')}</button>
              </form>
              {operationPending && <p role="status" className="mt-2 text-sm">{'讀取操作紀錄中…'}</p>}
              {operationError && <p role="alert" className="mt-2 text-sm text-amber-200">{operationError}</p>}
              {operationDetail && operationDetail.intent_id === selected.intent_id && <OperationDetails operation={operationDetail}/>}
            </section>
            {(listOffset >= MAX_INTENT_PAGE_OFFSET || memberOffset >= MAX_INTENT_PAGE_OFFSET) && <p className="mt-2 text-xs text-amber-200">{'已達有界分頁上限；不會載入全部紀錄。可使用需求 ID 查詢。'}</p>}
            <IntentReport intent={selected} memberPage={memberPage ?? undefined} now={evidenceNow}/>
            {memberPage && <div className="mt-3 flex flex-wrap items-center gap-3 text-sm"><span>{'成員分頁'}: {memberPage.items.length ? memberOffset + 1 : 0}–{memberOffset + memberPage.items.length} / {memberPage.total}</span><button type="button" disabled={memberOffset === 0} onClick={() => { setSelected(null); setMemberPage(null); setMemberOffset(Math.max(0, memberOffset - 100)); }} className="min-h-11 rounded border px-2 disabled:opacity-40">{'上一頁'}</button><button type="button" disabled={memberOffset + memberPage.items.length >= memberPage.total || memberOffset >= MAX_INTENT_PAGE_OFFSET} onClick={() => { setSelected(null); setMemberPage(null); setMemberOffset(memberOffset + 100); }} className="min-h-11 rounded border px-2 disabled:opacity-40">{'下一頁'}</button></div>}
          </>}
        </section>
      </div>
    </dialog>
  </>;
}
