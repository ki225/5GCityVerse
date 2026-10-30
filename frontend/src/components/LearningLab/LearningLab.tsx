import { useEffect, useState } from 'react';
import { CityNetworkMap } from '../../city-map/CityNetworkMap';
import { CityDemandConsole } from '../../city-map/components/CityDemandConsole';
import { useFormalIntent } from '../../city-map/context/FormalIntentContext';
import { useOnpremScenario } from '../../onprem/components/OnpremRuntime/ScenarioContext';
import { onpremJourneyStep } from '../../onprem/components/OnpremRuntime/ScenarioPanels';
import { NetworkAiPanel } from '../../onprem/components/OnpremRuntime/NetworkAiPanel';
import '../../city-map/styles/city-workspace.css';
import { IntentReport } from '../../onprem/components/IntentWorkspace/IntentReport';
import { CityMonitorProvider } from '../../city-map/context/CityMonitorContext';
import '../../city-map/styles/city-observability.css';
type Props = {
    runtimeBusy: boolean;
};
const STEPS = ['確認城市需求', '意圖與啟動', '網路與 AI', '比較成效'] as const;
export function formalJourneyStep(stage: string | undefined, operationStatus?: string, submitting = false): number {
    if (submitting)
        return 2;
    if (operationStatus && ['completed', 'failed', 'needs_definition', 'recovery_required'].includes(operationStatus))
        return 4;
    return stage ? onpremJourneyStep(stage) : operationStatus ? 3 : 1;
}
export function LearningLab({ runtimeBusy }: Props) {
    return <CityMonitorProvider><LearningWorkspace runtimeBusy={runtimeBusy}/></CityMonitorProvider>;
}
function LearningWorkspace({ runtimeBusy }: Props) {
    const adapter = useOnpremScenario()!, formal = useFormalIntent()!;
    const operation = formal.active?.latest_operation;
    const currentRound = Boolean(formal.submission?.traffic_request_id && adapter.snapshot?.requestId === formal.submission.traffic_request_id);
    const derived = formalJourneyStep(currentRound ? adapter.snapshot?.stage : undefined, operation?.status ?? formal.submission?.status, formal.pending || formal.uncertain);
    const [step, setStep] = useState(derived);
    useEffect(() => setStep(derived), [derived]);
    return <div className="city-lab-shell bg-slate-50 text-slate-900">
    <main className="city-layout-main">
      {runtimeBusy && <p role="status" className="mb-3 rounded bg-amber-50 p-3 text-sm">{'共享網路忙碌中；啟動仍需後端准入。'}</p>}
      <div data-testid="learning-workspace" className="city-experiment-workspace">
        <CityNetworkMap />
        <section className="city-flow-panel flex min-h-0 min-w-0 flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm" aria-label={'四步實驗流程'}>
          <div className="city-flow-heading"><b>城市需求與實驗</b><button type="button" onClick={() => formal.openWorkspace(formal.submission?.intent_id ?? '')}>服務意圖</button></div>
          <nav className="grid shrink-0 grid-cols-4 border-b border-slate-200 bg-slate-50" aria-label={'實驗步驟'}>
            {STEPS.map((label, index) => <button type="button" key={index} disabled={index > 1 && !formal.submission} onClick={() => setStep(index + 1)} aria-current={step === index + 1 ? 'step' : undefined} className={'min-h-12 px-1 py-2 text-xs disabled:opacity-40 ' + (step === index + 1 ? 'bg-primary-50 font-bold text-primary-700' : 'text-slate-600')}><span className="block">{index + 1}</span>{label}</button>)}
          </nav>
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain bg-slate-50/35 p-3 sm:p-4">
            <div hidden={step > 2}><CityDemandConsole phase={step === 1 ? 'review' : 'launch'} onNext={() => setStep(2)}/></div>
            <div hidden={step < 3} className="space-y-3">
              {formal.active ? <IntentReport intent={formal.active}/> : <p role="status" className="rounded border p-3 text-sm">{'等待這份正式需求的操作回報。'}</p>}
              {currentRound ? <NetworkAiPanel outcomes={step === 4}/> : <p className="rounded border border-amber-200 bg-amber-50 p-3 text-xs">{'尚未觀測到這份需求所屬的流量輪次；不顯示其他輪次作為本次結果。'}</p>}
            </div>
          </div>
        </section>
      </div>
    </main>

  </div>;
}
