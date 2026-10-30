import { useState } from 'react';
import { colors } from '../../../theme/colors';
import { SCENARIO_EVENTS, type ScenarioGroup } from '../../services/onpremScenarios';
import { comparableQos, type QosMetric, type QosSample, type RoundDecision } from '../../services/roundObservability';
import type { ScenarioAdapter } from './ScenarioContext';
export const observationTime = (at: string | number | null | undefined) => typeof at === 'number' ? (at < 1e12 ? at * 1000 : at) : at ? Date.parse(at) : NaN;
export function measurementSegments(samples: QosSample[], key: keyof QosSample): QosSample[][] {
    const segments: QosSample[][] = [];
    let segment: QosSample[] = [];
    for (const sample of samples) {
        const previous = segment.slice(-1)[0];
        const rtt = key === 'rttMs' && sample.rtt;
        const previousRtt = key === 'rttMs' && previous?.rtt;
        const started = rtt ? sample.rtt?.startedAt : sample.startedAt;
        const finished = previousRtt ? previous?.rtt?.finishedAt : previous?.finishedAt;
        const valid = sample.quality === 'observed' && typeof sample[key] === 'number' && Number.isFinite(observationTime(rtt ? sample.rtt?.finishedAt : sample.observedAt))
            && (!rtt || sample.rtt?.status === 'observed');
        const gap = previous && (!Number.isFinite(observationTime(started)) || !Number.isFinite(observationTime(finished))
            || observationTime(started) - observationTime(finished) > 1000
            || !previous.comparisonFingerprint || previous.comparisonFingerprint !== sample.comparisonFingerprint);
        if (!valid || gap) {
            if (segment.length)
                segments.push(segment);
            segment = [];
        }
        if (valid)
            segment.push(sample);
    }
    if (segment.length)
        segments.push(segment);
    return segments;
}
type Metric = {
    key: QosMetric;
    label: string;
    unit: string;
    color: string;
};
const format = (n: number | null | undefined) => n == null ? '—' : n.toLocaleString(undefined, { maximumFractionDigits: 4 });
const measured = (s: QosSample | null, key: QosMetric): number | null => s?.quality === 'observed'
    && (key !== 'rttMs' || !s.rtt || s.rtt.status === 'observed') ? s[key] : null;
const metricTime = (s: QosSample, key: QosMetric) => observationTime(key === 'rttMs' && s.rtt ? s.rtt.finishedAt : s.observedAt);
const displayTime = (at: string | null | undefined) => Number.isFinite(observationTime(at))
    ? new Intl.DateTimeFormat('zh-TW', { timeZone: 'Asia/Taipei', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).format(new Date(observationTime(at))) : '—';
function WindowTrack({ samples, decisions, selected, select, selectDecision }: {
    samples: QosSample[];
    decisions: RoundDecision[];
    selected: QosSample | null;
    select: (s: QosSample) => void;
    selectDecision: (d: RoundDecision) => void;
}) {
    const windows = samples.filter((s) => Number.isFinite(observationTime(s.startedAt)) && Number.isFinite(observationTime(s.finishedAt)));
    if (!windows.length)
        return null;
    const timedActions = decisions.map((d, i) => ({ d, i, at: observationTime(d.at) })).filter((a) => Number.isFinite(a.at));
    const times = [...windows.flatMap((s) => [observationTime(s.startedAt), observationTime(s.finishedAt)]), ...timedActions.map((a) => a.at)];
    const start = Math.min(...times), end = Math.max(start + 1000, ...times);
    const x = (at: number) => 36 + (at - start) / (end - start) * 528;
    return <div className="rounded-lg border border-slate-200 bg-slate-50 px-2 pt-2" data-testid="qos-window-track"><p className="text-[10px] text-slate-500">{'灰色區間未量測 · 綠線是決策紀錄時間，非政策生效時刻'}</p>
    <svg viewBox="0 0 600 88" className="w-full" role="img" aria-label={'量測窗口與決策紀錄時間'}>
      <path d="M36 47H564" stroke={colors.slate[300]} strokeWidth="10" strokeLinecap="round"/>
      {windows.map((s, i) => <g key={s.id} tabIndex={0} role="button" aria-label={`${'選擇量測'} ${i + 1}`} aria-pressed={selected?.id === s.id} onClick={() => select(s)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            select(s);
        } }} className="cursor-pointer">
        <rect x={x(observationTime(s.startedAt))} y="38" width={Math.max(2, x(observationTime(s.finishedAt)) - x(observationTime(s.startedAt)))} height="18" rx="3" fill={s.quality === 'observed' ? selected?.id === s.id ? colors.primary[600] : colors.slate[500] : colors.slate[200]}/>
        <text x={(x(observationTime(s.startedAt)) + x(observationTime(s.finishedAt))) / 2} y="31" textAnchor="middle" fontSize="11" fill={colors.slate[600]}>W{i + 1}</text>
        <title>{s.startedAt} → {s.finishedAt}</title>
      </g>)}
      {timedActions.map(({ d, i, at }) => <g key={d.id} tabIndex={0} role="button" aria-label={`A${i + 1} ${d.action ?? ''}`} onClick={() => selectDecision(d)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        selectDecision(d);
    } }} className="cursor-pointer"><path d={`M${x(at)} 28V62`} stroke={colors.primary[600]} strokeWidth="2"/><circle cx={x(at)} cy="24" r="9" fill={colors.primary[600]}/><text x={x(at)} y="13" textAnchor="middle" fontSize="11" fill={colors.primary[700]}>A{i + 1}</text><title>{d.at}: {d.action}</title></g>)}
      {[0, 0.5, 1].map((ratio) => <text key={ratio} x={36 + ratio * 528} y="78" textAnchor={ratio === 0 ? 'start' : ratio === 1 ? 'end' : 'middle'} fontSize="11" fill={colors.slate[500]}>{Math.round(ratio * (end - start) / 1000)}s</text>)}
    </svg>
  </div>;
}
function MetricComparison({ metric, before, selected, comparable }: {
    metric: Metric;
    before: QosSample | null;
    selected: QosSample;
    comparable: boolean;
}) {
    const a = measured(before, metric.key), b = measured(selected, metric.key);
    const same = before?.id === selected.id;
    const delta = comparable && !same && a !== null && b !== null ? b - a : null;
    const relative = delta !== null && a !== null && a > 0 && metric.key !== 'lossPercent' ? delta / a * 100 : null;
    const max = Math.max(a ?? 0, b ?? 0);
    const deltaText = delta === null ? null : `${delta > 0 ? '+' : ''}${delta.toLocaleString(undefined, { maximumSignificantDigits: 4 })} ${metric.key === 'lossPercent' ? '百分點' : metric.unit}`;
    return <article className="rounded-xl border border-slate-200 bg-white p-3" data-testid={`qos-compare-${metric.key}`} data-before-value={a ?? ''} data-after-value={b ?? ''} data-delta={delta ?? ''}>
    <div className="flex items-center justify-between gap-2"><h4 className="text-xs font-semibold text-slate-800">{metric.label}</h4><span className="text-[10px] text-slate-500">{metric.unit}</span></div>
    {a === null && b === null ? <p className="mt-3 text-xs text-slate-500" data-testid={`qos-unmeasured-${metric.key}`}>{'此指標未量測'}</p> : <>
      {([{ label: '基準', n: a, color: '#94a3b8' }, { label: '所選', n: b, color: metric.color }]).map(({ label, n, color }) => <div key={label} className="mt-3">
        <div className="flex items-baseline justify-between gap-2 text-[10px]"><span className="text-slate-500">{label}</span><span className="font-mono font-semibold tabular-nums text-slate-800" title={n === null ? undefined : String(n)}>{format(n)} {n !== null && metric.unit}</span></div>
        {n === null ? <p className="mt-1 text-[10px] text-slate-400">{'未量測'}</p> : <div className="mt-1 h-2 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full" style={{ width: `${max > 0 ? n / max * 100 : 0}%`, backgroundColor: color }}/></div>}
      </div>)}
      <p className="mt-3 border-t border-slate-100 pt-2 text-[10px] text-slate-600" data-testid={`qos-delta-${metric.key}`}>
        {deltaText !== null ? <><span className="font-semibold text-slate-800">Δ {deltaText}</span>{relative !== null && <span> ({relative > 0 ? '+' : ''}{relative.toLocaleString(undefined, { maximumSignificantDigits: 3 })}%)</span>}</>
                : same ? '同一量測窗口' : '缺少可比較的量測'}
      </p>
    </>}
  </article>;
}
function MetricHistory({ metric, samples, selected, select }: {
    metric: Metric;
    samples: QosSample[];
    selected: QosSample;
    select: (s: QosSample) => void;
}) {
    const [hovered, setHovered] = useState<string | null>(null);
    const valid = samples.filter((s) => measured(s, metric.key) !== null && Number.isFinite(metricTime(s, metric.key)));
    const detail = valid.find((s) => s.id === hovered) ?? valid.find((s) => s.id === selected.id) ?? valid.slice(-1)[0];
    if (!valid.length)
        return <div className="rounded-lg border border-dashed border-slate-200 p-3 text-xs text-slate-500" data-testid={`qos-unmeasured-${metric.key}`}><b>{metric.label}</b> · {'此指標未量測'}</div>;
    const times = samples.flatMap((s) => [observationTime(s.startedAt), observationTime(s.finishedAt), metricTime(s, metric.key)]).filter(Number.isFinite);
    const start = Math.min(...times), end = Math.max(start + 1000, ...times);
    const peak = Math.max(...valid.map((s) => s[metric.key]!), 0.001);
    const step = 10 ** Math.floor(Math.log10(peak)) / 2;
    const max = Math.ceil(peak * 1.1 / step) * step;
    const x = (s: QosSample) => 66 + (metricTime(s, metric.key) - start) / (end - start) * 490;
    const y = (n: number) => 128 - n / max * 94;
    const origin = start;
    return <article className="rounded-xl border border-slate-200 bg-white p-3" data-testid={`qos-history-${metric.key}`}>
    <div className="flex items-center justify-between gap-2 text-xs"><h4 className="font-semibold">{metric.label}</h4><span className="text-[10px] text-slate-500">{metric.unit}</span></div>
    <svg viewBox="0 0 600 170" className="mt-2 w-full" role="img" aria-label={`${metric.label} ${'量測時間軸'}`}>
      {[0, 0.5, 1].map((ratio) => <g key={ratio}><path d={`M66 ${y(max * ratio)}H556`} stroke={colors.slate[200]} strokeDasharray="3 3"/><text x="58" y={y(max * ratio) + 4} textAnchor="end" fontSize="11" fill={colors.slate[500]}>{format(max * ratio)}</text></g>)}
      <path d="M66 30V128H556" fill="none" stroke={colors.slate[300]}/>
      {measurementSegments(samples, metric.key).filter((segment) => segment.length > 1).map((segment, i) => <polyline key={i} points={segment.map((s) => `${x(s)},${y(s[metric.key]!)}`).join(' ')} fill="none" stroke={metric.color} strokeWidth="2"/>)}
      {valid.map((s) => <g key={s.id}>
        {s.id === selected.id && <path d={`M${x(s)} 30V128`} stroke={metric.color} strokeDasharray="3 3" opacity="0.3"/>}
        <circle cx={x(s)} cy={y(s[metric.key]!)} r={s.id === selected.id ? 6 : 4} stroke="white" strokeWidth="2" fill={metric.color}/>
        <circle data-testid="qos-history-point" data-sample-id={s.id} data-metric={metric.key} cx={x(s)} cy={y(s[metric.key]!)} r="11" fill="transparent" tabIndex={0} role="button" aria-label={`${s.stage} ${s.observedAt} ${metric.key} ${s[metric.key]}`} aria-pressed={s.id === selected.id} onMouseEnter={() => setHovered(s.id)} onMouseLeave={() => setHovered(null)} onFocus={() => setHovered(s.id)} onBlur={() => setHovered(null)} onClick={() => select(s)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            select(s);
        } }}>
          <title>{s.observedAt}: {s[metric.key]} {metric.unit}</title>
        </circle>
      </g>)}
      {[0, 0.5, 1].map((ratio) => <text key={ratio} x={66 + ratio * 490} y="151" fontSize="11" fill={colors.slate[500]} textAnchor={ratio === 0 ? 'start' : ratio === 1 ? 'end' : 'middle'}>{((start + ratio * (end - start) - origin) / 1000).toFixed(0)}s</text>)}
    </svg>
    <p className="min-h-8 text-[10px] text-slate-600" data-testid={`qos-point-detail-${metric.key}`}><b style={{ color: metric.color }}>{detail ? format(detail[metric.key]) : '—'} {metric.unit}</b> · {detail ? displayTime(detail.observedAt) + ' UTC+8' : '—'}<br />{'點選數值點，可同步查看該窗口。未量測的期間不連線。'}</p>
  </article>;
}
export function QosTimeline({ adapter }: {
    adapter: ScenarioAdapter;
}) {
    const [relative, setRelative] = useState<'baseline' | 'previous'>('baseline');
    const [view, setView] = useState<'comparison' | 'timeline'>('comparison');
    const scenario = adapter.selectedScenario ?? 'web_browsing';
    const samples = (adapter.observation?.samples ?? []).filter((s) => s.scenario === scenario).sort((a, b) => observationTime(a.observedAt) - observationTime(b.observedAt));
    const selected = samples.find((s) => s.id === adapter.selectedSampleId) ?? samples.slice(-1)[0] ?? null;
    const previous = selected ? samples.filter((s) => s.sequence < selected.sequence && s.quality === 'observed').slice(-1)[0] ?? null : null;
    const baseline = samples.find((s) => s.stage === 'measuring_baseline' && s.quality === 'observed') ?? null;
    const ids = new Set(samples.map((s) => s.id));
    const decisions = (adapter.observation?.data?.decisions ?? []).filter((d) => d.scenario === scenario || (!d.scenario && [...(d.beforeSampleIds ?? []), ...(d.afterSampleIds ?? [])].some((id) => ids.has(id))));
    const selectedDecision = decisions.find((d) => d.id === adapter.selectedDecisionId);
    const actionBefore = samples.find((s) => selectedDecision?.beforeSampleIds?.includes(s.id)) ?? null;
    const before = relative === 'baseline' ? baseline : actionBefore ?? previous;
    const comparable = comparableQos(before, selected);
    const select = (s: QosSample) => { adapter.selectSample?.(s.id); adapter.selectDecision?.(null); };
    const selectDecision = (d: RoundDecision) => { adapter.selectDecision?.(d.id); setRelative('previous'); const s = samples.find((s) => d.afterSampleIds?.includes(s.id)) ?? samples.find((s) => d.beforeSampleIds?.includes(s.id)); adapter.selectSample?.(s?.id ?? null); };
    const stage = (s: QosSample) => s.stage === 'measuring_baseline' ? '基準量測' : s.stage === 'measuring_result' ? '結果量測' : '量測';
    const metrics: Metric[] = [
        { key: 'receivedUplinkMbps', label: '接收 UL', unit: 'Mbps', color: '#059669' },
        { key: 'receivedDownlinkMbps', label: '接收 DL', unit: 'Mbps', color: colors.primary[600] },
        { key: 'lossPercent', label: '接收端丟包率', unit: '%', color: '#dc2626' },
        { key: 'rttMs', label: '平均 RTT · 獨立探測', unit: 'ms', color: colors.primary[600] },
    ];
    return <section className="space-y-3 rounded-xl border border-slate-200 bg-white p-3" data-testid="qos-timeline" data-chart-view={view}>
    <header className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-sm font-bold">{'QoS 量測比較'}</h3>
      <label className="text-[10px] text-slate-500">{'情境'} <select aria-label={'曲線情境'} value={scenario} onChange={(e) => adapter.selectScenario?.(e.target.value as ScenarioGroup)} className="rounded border border-slate-200 p-1 text-slate-800">{(['web_browsing', ...SCENARIO_EVENTS] as ScenarioGroup[]).map((group) => <option key={group} value={group}>{group}</option>)}</select></label>
    </header>
    {!samples.length ? <p className="py-8 text-center text-xs text-slate-500" data-testid="qos-timeline-empty">{'尚無歷史量測窗口；取得實測資料後顯示圖表。'}</p> : <>
      <div className="flex flex-wrap items-center justify-between gap-2"><div className="inline-flex rounded-lg bg-slate-100 p-1" role="group" aria-label={'QoS 圖表呈現'}>
        {(['comparison', 'timeline'] as const).map((mode) => <button key={mode} type="button" aria-pressed={view === mode} onClick={() => setView(mode)} className={`min-h-8 rounded-md px-3 text-xs ${view === mode ? 'bg-white font-semibold text-primary-800 shadow-sm' : 'text-slate-600'}`}>{mode === 'comparison' ? '前後比較' : '量測時間軸'}</button>)}
      </div><span className="text-[10px] text-slate-500">{samples.filter((s) => s.quality === 'observed').length} {'個有效量測窗口'} · {'歷史資料'}</span></div>
      <div className="rounded-lg border border-primary-100 bg-primary-50/50 p-2">
        <div className="flex flex-wrap items-center justify-between gap-2"><b className="text-[10px] text-slate-600">{'選擇量測窗口'}</b><label className="text-[10px] text-slate-500">{'比較基準'} <select aria-label={'比較基準'} className="rounded border border-slate-200 bg-white p-1 text-slate-800" value={relative} onChange={(e) => setRelative(e.target.value as typeof relative)}><option value="baseline">{'原始基準'}</option><option value="previous">{'上一次量測'}</option></select></label></div>
        <div className="mt-2 flex flex-wrap gap-2">{samples.map((s, i) => <button key={s.id} type="button" onClick={() => select(s)} aria-pressed={s.id === selected?.id} data-testid="qos-window-select" data-sample-id={s.id} className={`min-h-10 rounded-lg border px-3 py-2 text-left text-[10px] ${s.id === selected?.id ? 'border-primary-400 bg-white text-primary-900 ring-1 ring-primary-200' : 'border-slate-200 bg-white/70 text-slate-600'}`}><span className="font-semibold">W{i + 1}. {stage(s)}</span><br /><span className="font-mono tabular-nums">{displayTime(s.observedAt)} UTC+8</span>{s.quality !== 'observed' && <span> · {'缺測'}</span>}</button>)}</div>
      </div>
      <WindowTrack samples={samples} decisions={decisions} selected={selected} select={select} selectDecision={selectDecision}/>
      {selected && <>
        <div data-testid="qos-comparison"><p className="text-[10px] text-slate-600">{'基準'}: {before ? stage(before) : '—'} → {'所選窗口'}: {stage(selected)}</p>
          <div className={view === 'comparison' ? 'mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2' : 'mt-2 space-y-3'}>
            {metrics.map((metric) => view === 'comparison' ? <MetricComparison key={metric.key} metric={metric} before={before} selected={selected} comparable={comparable}/> : <MetricHistory key={metric.key} metric={metric} samples={samples} selected={selected} select={select}/>)}
          </div>
          <p className={`mt-2 text-[10px] ${comparable ? 'text-slate-500' : 'text-amber-800'}`}>{comparable ? '同負載窗口的觀測差異；並非 AI 因果效果判定。' : '不可比較：窗口、負載識別或量測證據不足。'}</p>
        </div>
        <details className="rounded-lg border border-slate-200 text-[10px] text-slate-500"><summary className="cursor-pointer p-2">{'量測時間與來源'}</summary><div className="space-y-2 border-t border-slate-100 p-2">{[{ label: '基準', sample: before }, { label: '所選', sample: selected }].map(({ label, sample: s }) => s && <p key={label} className="break-all">{label}: {s.startedAt ?? '—'} → {s.finishedAt ?? '—'}<br />{s.source} · {s.sampleCount} UE · {s.id}</p>)}</div></details>
      </>}
      <div className="border-t border-slate-100 pt-3"><h4 className="text-[10px] font-semibold text-slate-600">{'對應 AI 決策'}</h4><div className="mt-2 flex flex-wrap gap-2">{decisions.map((d, i) => <button key={d.id} type="button" onClick={() => selectDecision(d)} aria-pressed={adapter.selectedDecisionId === d.id} className={`min-h-9 rounded-lg border px-3 text-[10px] ${adapter.selectedDecisionId === d.id ? 'border-primary-400 bg-primary-50 text-primary-800' : 'border-slate-200 text-slate-600'}`}>A{i + 1} · {d.action === 'hold' ? '維持現況' : d.action} · {d.at ? displayTime(d.at) + ' UTC+8' : '時間未記錄'}</button>)}{!decisions.length && <p className="text-[10px] text-slate-500">{'尚無連結此情境量測的 AI 決策。'}</p>}</div></div>
    </>}
  </section>;
}
