import type { ServiceSlaAssessment, SlaStatus } from '../../services/serviceRequirements';
export function ServiceSlaResults({ assessment }: {
    assessment: ServiceSlaAssessment;
}) {
    const state = (s: SlaStatus) => s === 'met' ? '達標' : s === 'not_met' ? '未達標' : '未知';
    const metricNames = { receivedUplinkMbps: 'UL Mbps', receivedDownlinkMbps: 'DL Mbps', rttMs: 'RTT ms', lossPercent: '丟包 %' };
    const reason = (code?: string) => ({ same_window_rtt_unavailable: '沒有同視窗 RTT 證據', interval_evidence_missing: '區間量測不足', packet_denominator_missing: '封包計數不足', offered_load_insufficient: '實際發送負載不足', criteria_evaluated: '已依需求評估' }[code ?? ''] ?? '證據不足');
    return <section data-testid="service-sla-results" className="mt-3 space-y-3 border-t border-slate-300 pt-3 font-normal"><p className="break-all text-xs">{'需求版本'}: {assessment.revision}</p>
    {[assessment.latestWindow, assessment.wholeRound].map((window, index) => <section key={index} className="space-y-2"><h4 className="text-sm font-bold">{index === 0 ? '最近完整評估視窗' : '整輪已觀測服務期間（含量測缺口）'}: {state(window.status)}</h4>
      <p className="text-xs">{window.startedAt == null ? '—' : new Date(window.startedAt * 1000).toLocaleString()} – {window.finishedAt == null ? '—' : new Date(window.finishedAt * 1000).toLocaleString()} · {window.complete ? '視窗完整' : '尚未完整'}</p>
      {[...window.services, ...window.slices].map((r) => <article key={r.scenario ?? r.slice} className="rounded border border-slate-200 bg-white p-2 text-xs"><b>{r.scenario ?? r.slice} · {r.scope} · {state(r.status)}</b><p className="mt-1">{'要求'}: {r.evaluation.windowSeconds}s / {r.evaluation.sampleIntervalSeconds}s · {'時間達標'} ≥ {r.evaluation.minTimeCompliance * 100}%{r.scope === 'per-ue' && ` · UE ≥ ${r.evaluation.minUeCompliance * 100}%`}</p><p>{new Date(r.startedAt * 1000).toLocaleString()} – {new Date(r.finishedAt * 1000).toLocaleString()}</p><p>{'預期／有身分紀錄 UE'}: {r.expectedUes} / {r.observedUes}{r.passedUes !== undefined && ` · ${'達標／失敗／未知 UE'}: ${r.passedUes}/${r.failedUes}/${r.unknownUes}`}</p>
        <div className="overflow-x-auto"><table className="mt-2 w-full text-left"><thead><tr><th>{'指標'}</th><th>{'需求'}</th><th>{'量測平均／每 UE 平均範圍'}</th><th>{'覆蓋率'}</th><th>{'判定'}</th></tr></thead><tbody>{Object.entries(r.metrics).map(([key, m]) => <tr key={key}><td>{metricNames[key as keyof typeof metricNames]}</td><td>{key.startsWith('received') ? '≥' : '≤'} {m.target}</td><td>{r.scope === 'per-ue' ? m.observedMin == null || m.observedMax == null ? '—' : `${m.observedMin.toFixed(3)} – ${m.observedMax.toFixed(3)}` : m.observedMean == null ? '—' : m.observedMean.toFixed(3)}</td><td>{(m.coverageRatio * 100).toFixed(1)}% {r.scope === 'per-ue' ? 'UE' : '時間'}</td><td>{state(m.status)} · {reason(m.reason)}</td></tr>)}</tbody></table></div>
        {r.perUe && <details className="mt-2"><summary>{'個別 UE 與時間覆蓋率'}</summary>{r.perUe.map((ue) => <p key={ue.ueRef} className="my-1">{ue.ueRef} · {state(ue.status)} · {Object.entries(ue.metrics).map(([k, m]) => `${metricNames[k as keyof typeof metricNames]}: ${m.observedMean == null ? '—' : m.observedMean.toFixed(3)} / ${Math.round(m.coverageRatio * 100)}% / ${state(m.status)}`).join(' · ')}</p>)}</details>}
      </article>)}
      {!window.services.length && !window.slices.length && <p className="text-xs">{'尚無可評估的原生量測或未設定 SLA。'}</p>}
    </section>)}
  </section>;
}
