import { createContext, useContext, useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { BootstrapRequestError, nativeDeviceAdmissionReady, operatorZeroResidentViewAllowed, requestBootstrapStatus, type BootstrapStage, type BootstrapStatus } from '../../services/onpremBootstrap';
const STAGES: BootstrapStage[] = ['core', 'gnb', 'ue', 'pdu'];
const POLL_INTERVAL_MS = 2000;
const REQUEST_TIMEOUT_MS = 10000;
const STALLED_STAGE_MS = 180000;
const NativeExecutionReady = createContext(false);
export const useNativeExecutionReady = () => useContext(NativeExecutionReady);
export function retryableFailureStartedAt(status: BootstrapStatus, firstSeenAt: number | null, now: number): number | null {
    if (status.state !== 'failed' || !status.retryable)
        return null;
    const startedAt = firstSeenAt ?? now;
    return now - startedAt < STALLED_STAGE_MS ? startedAt : null;
}
function stageLabel(stage: BootstrapStage): string {
    const labels = {
        core: '5G 核心網路',
        gnb: 'gNB 基地台接入',
        ue: '市民 UE 註冊',
        pdu: 'PDU 與連通性驗證',
    };
    return labels[stage];
}
export function CityBootstrapGate({ children }: {
    children: ReactNode;
}) {
    const [status, setStatus] = useState<BootstrapStatus | null>(null);
    const [problem, setProblem] = useState('');
    const [unreachable, setUnreachable] = useState(false);
    const [checking, setChecking] = useState(true);
    const [submitting, setSubmitting] = useState(false);
    const [operatorCalibrationView, setOperatorCalibrationView] = useState(false);
    const [attempt, setAttempt] = useState(0);
    const [draftOpen, setDraftOpen] = useState(true);
    const [admissionNow, setAdmissionNow] = useState(Date.now());
    const lastProgress = useRef({ key: '', at: Date.now() });
    const initialized = useRef(false);
    const refresh = useCallback(() => {
        initialized.current = false;
        setProblem('');
        setUnreachable(false);
        setChecking(true);
        lastProgress.current = { key: '', at: Date.now() };
        setAttempt((current) => current + 1);
    }, []);
    useEffect(() => {
        // Initialization runs once per mounted city to preserve the accepted UE generation.
        if (initialized.current)
            return;
        let stopped = false;
        let pollTimer: ReturnType<typeof setTimeout> | undefined;
        let active: AbortController | undefined;
        let retryableFailureAt: number | null = null;
        function pollRetryableFailure(next: BootstrapStatus) {
            const startedAt = retryableFailureStartedAt(next, retryableFailureAt, Date.now());
            if (startedAt === null)
                return;
            retryableFailureAt = startedAt;
            pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
        }
        async function poll() {
            const controller = new AbortController();
            active = controller;
            const requestTimer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
            try {
                const next = await requestBootstrapStatus('GET', controller.signal);
                if (stopped)
                    return;
                const mode = typeof window === 'undefined' ? null
                    : window.location.search === '?operator-calibration=1' ? 'calibration'
                        : window.location.search === '?operator-ai100=1' ? 'ai100' : null;
                let operatorViewPending = false;
                let allowed = false;
                if (mode) {
                    allowed = await operatorZeroResidentViewAllowed(next, mode, controller.signal);
                    if (stopped)
                        return;
                    operatorViewPending = !allowed && next.state === 'absent' && next.stage === 'ue';
                }
                // Publish one complete bootstrap result after all required reads.
                // Intermediate false values would tear down the form mid-submission.
                initialized.current = next.state === 'ready' || allowed;
                // Keep monitoring/release controls mounted after a real start.
                // Losing new-admission readiness only disables the next submission.
                if (next.state === 'ready' || allowed || nativeDeviceAdmissionReady(next))
                    setDraftOpen(true);
                setStatus(next);
                setOperatorCalibrationView(allowed);
                setUnreachable(false);
                setChecking(false);
                setProblem('');
                if (next.state !== 'failed')
                    retryableFailureAt = null;
                if (operatorViewPending) {
                    // A successful bootstrap read is not a successful zero-fleet proof.
                    // Retry reads only, within the existing three-minute recovery window.
                    if (Date.now() - lastProgress.current.at < STALLED_STAGE_MS) {
                        pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
                    }
                    else {
                        setProblem('尚未確認零 UE 與前次清理狀態，請重新查詢。');
                    }
                    return;
                }
                if (next.state === 'starting') {
                    retryableFailureAt = null;
                    const key = `${next.state}:${next.stage}`;
                    if (key !== lastProgress.current.key)
                        lastProgress.current = { key, at: Date.now() };
                    if (Date.now() - lastProgress.current.at >= STALLED_STAGE_MS) {
                        setProblem(`${next.stage ? stageLabel(next.stage) : '初始化'} 已超過三分鐘沒有階段進展。請檢查後端狀態，再重新查詢。`);
                        return;
                    }
                    pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
                }
                else if (next.state === 'failed') {
                    pollRetryableFailure(next);
                }
                else if (next.state === 'ready' || (next.state === 'absent' && next.stage === 'ue') || allowed) {
                    // Readiness is revocable: never retain a pre-retirement ready sample.
                    pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
                }
            }
            catch (error) {
                if (!stopped) {
                    setChecking(false);
                    setProblem(error instanceof BootstrapRequestError
                        ? error.message
                        : '無法連線至後端，或狀態查詢逾時。請確認後端運行，再重新查詢。');
                    setUnreachable(!(error instanceof BootstrapRequestError && error.status));
                    if (error instanceof BootstrapRequestError && error.status) {
                        setStatus(error.status);
                        pollRetryableFailure(error.status);
                    }
                    else {
                        // A restart/timeout revokes execution immediately, but
                        // bounded read retries preserve the user's draft and token.
                        retryableFailureAt = retryableFailureAt ?? Date.now();
                        if (Date.now() - retryableFailureAt < STALLED_STAGE_MS)
                            pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
                    }
                }
            }
            finally {
                clearTimeout(requestTimer);
            }
        }
        void poll();
        return () => { stopped = true; active?.abort(); clearTimeout(pollTimer); };
    }, [attempt]);
    useEffect(() => {
        const timer = setInterval(() => setAdmissionNow(Date.now()), 1000);
        return () => clearInterval(timer);
    }, []);
    async function start() {
        if (submitting)
            return;
        setSubmitting(true);
        setProblem('');
        setUnreachable(false);
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
        try {
            const next = await requestBootstrapStatus('POST', controller.signal);
            setStatus(next);
            refresh();
        }
        catch (error) {
            setProblem(error instanceof BootstrapRequestError
                ? error.message
                : '啟動請求未得到後端回應。請先重新查詢狀態，避免重複啟動。');
            setUnreachable(!(error instanceof BootstrapRequestError && error.status));
            if (error instanceof BootstrapRequestError && error.status)
                setStatus(error.status);
        }
        finally {
            clearTimeout(timer);
            setSubmitting(false);
        }
    }
    const deviceReady = !checking && nativeDeviceAdmissionReady(status, admissionNow);
    const executionReady = Boolean(!checking && (status?.state === 'ready' || operatorCalibrationView || deviceReady) && !problem);
    if (executionReady || draftOpen)
        return <NativeExecutionReady.Provider value={executionReady}>
          {deviceReady && <section data-testid="city-device-admission-ready" className="city-network-notice">
            網路可建立新 UE 並開始實驗；目前沒有已建立的 UE／PDU。容量與服務效果尚未驗證。
          </section>}
          {!executionReady && <section data-testid="city-draft-only" className="city-network-notice">
            <details><summary>{checking ? '正在確認網路狀態' : '網路尚未就緒 · 可編輯城市需求'}</summary>
              <div className="city-network-notice-detail"><p>可以點選建築設定需求；網路就緒前不會啟動 UE 或 AI，也不代表即時網路狀態。</p>
                {!checking && <p>{problem || status?.error?.detail || status?.message || '尚未取得網路就緒狀態。'}</p>}
                <button type="button" onClick={refresh}>重新查詢網路狀態</button>
                <button type="button" onClick={() => setDraftOpen(false)}>查看網路初始化狀態</button>
              </div>
            </details>
          </section>}
          {children}
        </NativeExecutionReady.Provider>;
    const currentIndex = status?.stage ? STAGES.indexOf(status.stage) : -1;
    const failed = status?.state === 'failed';
    const absent = status?.state === 'absent';
    const starting = status?.state === 'starting' && !problem;
    const detail = status?.error?.detail || status?.message;
    return <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-slate-950 px-4 py-10 text-white">
    <div aria-hidden="true" className="absolute inset-0 bg-[radial-gradient(circle_at_50%_35%,rgba(14,165,233,.22),transparent_45%)]"/>
    <section aria-labelledby="city-bootstrap-title" className="relative w-full max-w-3xl rounded-3xl border border-primary-400/30 bg-slate-900/90 p-6 shadow-[0_30px_100px_rgba(0,0,0,.45)] sm:p-10">
      <p className="text-xs font-bold uppercase tracking-[.28em] text-primary-300">5GCityVerse / On-prem</p>
      <h1 id="city-bootstrap-title" className="mt-3 text-3xl font-bold sm:text-4xl">{'啟動你的 5G 城市'}</h1>
      <p className="mt-3 text-sm leading-6 text-slate-300">{checking && !problem ? ('正在確認城市狀態…') : failed || problem ? ('城市尚未就緒；請依下方資訊處理後重試。') : absent ? ('目前尚未建立城市資源。按下按鈕後才會開始建置。') : ('資源正在建立；此畫面會依後端實際狀態更新。')}</p>

      <div className="relative mt-8 rounded-2xl border border-primary-400/20 bg-slate-950/70 px-6 pb-3 pt-8" aria-hidden="true">
        <svg viewBox="0 0 640 150" className="h-32 w-full text-primary-400" fill="none">
          <path d="M0 134H640M62 134V76h40v58M112 134V47h39v87M162 134V89h47v45M220 134V61h52v73M282 134V29h43v105M337 134V74h45v60M395 134V42h50v92M457 134V83h47v51M518 134V58h48v76M579 134V96h36v38" stroke="currentColor" strokeOpacity=".58" strokeWidth="3"/>
          <path d="M60 113C180 24 259 78 322 24s154 18 262 79" stroke="currentColor" strokeOpacity=".6" strokeDasharray="8 8" strokeWidth="2"/>
          {[130, 320, 510].map((x) => <circle key={x} cx={x} cy={x === 320 ? 26 : 79} r="7" fill="currentColor" className={starting ? 'motion-safe:animate-pulse' : ''}/>)}
        </svg>
      </div>

      <ol className="mt-7 grid gap-3 sm:grid-cols-4" aria-label={'城市初始化階段'}>
        {STAGES.map((stage, index) => {
            const active = starting && index === currentIndex;
            const complete = starting && currentIndex > index;
            const stageFailed = failed && stage === status?.stage;
            return <li key={stage} className={`rounded-xl border px-4 py-3 text-sm ${stageFailed ? 'border-red-400/60 bg-red-950/40' : active ? 'border-primary-300 bg-primary-900/40' : complete ? 'border-emerald-500/40 bg-emerald-950/30' : 'border-slate-700 bg-slate-800/60'}`}>
            <span className="block text-xs text-slate-400">{String(index + 1).padStart(2, '0')}</span>
            <span className="mt-1 block font-semibold">{stageLabel(stage)}</span>
            <span className="mt-1 block text-xs text-slate-300">{stageFailed ? ('失敗') : active ? ('進行中') : complete ? ('已完成') : ('待處理')}</span>
          </li>;
        })}
      </ol>

      {(problem || failed) && <div role="alert" className="mt-6 rounded-xl border border-red-400/50 bg-red-950/50 p-4 text-sm text-red-100">
        <p className="font-bold">{'無法完成城市啟動'}</p>
        <p className="mt-1 break-words">{problem || detail || ('後端未提供失敗原因。')}</p>
        {status?.stage && <p className="mt-2">{'失敗階段：'}{stageLabel(status.stage)}</p>}
        {failed && !status?.retryable && <p className="mt-2">{'此錯誤無法在網頁上重試。請交由系統管理者依上述原因完成網路資源設定或修復，再重新查詢狀態。'}</p>}
      </div>}

      {starting && <p role="status" className="mt-5 text-sm text-primary-200">{detail || ('等待後端回報進度…')}</p>}
      <div className="mt-6 flex flex-wrap gap-3">
        <button type="button" onClick={() => setDraftOpen(true)} className="min-h-11 rounded-xl border border-slate-400 px-6 py-3 font-bold text-white hover:bg-slate-800">先編輯城市需求</button>
        {absent && !problem && <button type="button" onClick={() => void start()} disabled={submitting || checking} className="rounded-xl bg-primary-500 px-6 py-3 font-bold text-slate-950 hover:bg-primary-400 disabled:opacity-50">{submitting ? ('送出啟動請求…') : ('啟動城市')}</button>}
        {(problem || failed) && <button type="button" onClick={refresh} className="rounded-xl border border-primary-300 px-6 py-3 font-bold text-primary-100 hover:bg-primary-900/40">{'重新查詢狀態'}</button>}
        {failed && status?.retryable && !unreachable && <button type="button" onClick={() => void start()} disabled={submitting} className="rounded-xl bg-primary-500 px-6 py-3 font-bold text-slate-950 hover:bg-primary-400 disabled:opacity-50">{'重試啟動'}</button>}
      </div>
    </section>
  </main>;
}
