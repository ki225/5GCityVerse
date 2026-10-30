import { useEffect, useState } from 'react';
import { useAppStore } from '../../../store/appStore';
import { getDailyNativeObservation, getRuntimeStatus, requestDailyTrend, type DailyNativeObservation, type DailyTrend, } from '../../../services/api';
export function currentDailyTrend(observation: DailyNativeObservation, trend: DailyTrend | null): DailyTrend | null {
    return observation.available && observation.snapshotId === trend?.snapshotId ? trend : null;
}
const DAILY_SLICES = ['embb', 'urllc', 'mmtc', 'v2x'] as const;
export function dailySliceCount(observation: DailyNativeObservation | null, slice: typeof DAILY_SLICES[number]): number {
    const value = observation?.slices?.[slice]?.ueCount;
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0;
}
export function compactObservationSource(observation: DailyNativeObservation | null): string {
    return JSON.stringify(observation?.source ?? null);
}
export function failedDailyTrend(cause: unknown): {
    trend: null;
    error: string;
} {
    return { trend: null, error: String(cause) };
}
export function DailyNativeObservationPanel({ placement = 'floating' }: {
    placement?: 'floating' | 'inline';
}) {
    const setRuntimeStatus = useAppStore((state) => state.setRuntimeStatus);
    const [observation, setObservation] = useState<DailyNativeObservation | null>(null);
    const [trend, setTrend] = useState<DailyTrend | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [planning, setPlanning] = useState(false);
    useEffect(() => {
        let stopped = false;
        let inFlight = false;
        const refresh = async () => {
            if (stopped || inFlight)
                return;
            inFlight = true;
            try {
                const value = await getDailyNativeObservation();
                if (!stopped) {
                    setObservation(value);
                    setTrend((current) => currentDailyTrend(value, current));
                    setError(value.available ? null : (value.reason || 'Daily native evidence unavailable'));
                }
            }
            catch (cause) {
                if (!stopped)
                    setError(String(cause));
            }
            finally {
                inFlight = false;
            }
        };
        void refresh();
        const timer = window.setInterval(refresh, 15000);
        return () => { stopped = true; window.clearInterval(timer); };
    }, []);
    const analyze = async () => {
        setPlanning(true);
        setError(null);
        try {
            const value = await requestDailyTrend();
            setTrend(value);
            // The header capability badge is a separate runtime snapshot. Refresh
            // it after the successful AI call, without turning a badge-fetch error
            // into a false failure of the already accepted trend.
            try {
                setRuntimeStatus(await getRuntimeStatus());
            }
            catch { /* next runtime poll may recover */ }
            const latest = await getDailyNativeObservation();
            setObservation(latest);
            setTrend((current) => currentDailyTrend(latest, current));
        }
        catch (cause) {
            const failed = failedDailyTrend(cause);
            setTrend(failed.trend);
            setError(failed.error);
        }
        finally {
            setPlanning(false);
        }
    };
    const available = observation?.available === true;
    return (<aside aria-label={'城市日常原生觀測'} data-testid="daily-native-observation" data-snapshot-id={observation?.snapshotId ?? ''} data-observed-at={observation?.observedAt ?? ''} data-source={compactObservationSource(observation)} data-available={String(available)} data-reason={observation?.reason ?? ''} className={`${placement === 'floating' ? 'fixed bottom-4 right-4 z-40 w-[min(24rem,calc(100vw-2rem))]' : 'w-full'} rounded-xl border border-slate-600 bg-slate-950/95 p-4 text-sm text-slate-100 shadow-xl`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="font-bold">{'城市日常觀測'}</h2>
          <p data-testid="daily-native-availability" className={available ? 'text-emerald-300' : 'text-amber-300'}>
            {available ? ('原生證據可用') : ('原生證據不可用')}
          </p>
        </div>
        <button type="button" disabled={!available || planning} onClick={() => void analyze()} className="rounded-md bg-primary-600 px-3 py-2 font-semibold disabled:cursor-not-allowed disabled:bg-slate-700">
          {planning ? ('分析中…') : ('AI 趨勢')}
        </button>
      </div>
      {observation?.observedAt && <p className="mt-2 text-xs text-slate-400">{observation.observedAt} · {observation.ueCount ?? observation.ues.length} UE</p>}
      <div className="mt-2 grid grid-cols-4 gap-1" aria-label={'各網路切片 UE 數量'}>
        {DAILY_SLICES.map((slice) => {
            const count = dailySliceCount(observation, slice);
            return <span key={slice} data-testid={`daily-native-slice-${slice}`} data-count={String(count)} className="rounded bg-slate-800 px-1 py-1 text-center text-xs">
            <span className="block uppercase text-slate-400">{slice}</span>{count} UE
          </span>;
        })}
      </div>
      <p className="mt-1 text-xs text-slate-400">{'僅供群體趨勢；不可授權特定 UE 操作'}</p>
      {!available && observation?.reason && <p data-testid="daily-native-reason" className="mt-2 text-xs text-amber-300">{observation.reason}</p>}
      {trend && <p data-testid="daily-native-trend" data-snapshot-id={trend.snapshotId} data-observed-at={trend.observedAt} className="mt-3 rounded-md bg-slate-800 p-3">{trend.trend}</p>}
      {error && <p role="alert" className="mt-2 text-xs text-rose-300">{error}</p>}
    </aside>);
}
