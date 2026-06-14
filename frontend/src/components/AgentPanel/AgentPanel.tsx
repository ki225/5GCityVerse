import { useAppStore } from '../../store/appStore'
import type { AgentAction, RiskLevel } from '../../types'

const RISK_COLOR: Record<RiskLevel, string> = {
  low:      'text-green-400 bg-green-900/40 border-green-700',
  medium:   'text-yellow-400 bg-yellow-900/40 border-yellow-700',
  high:     'text-orange-400 bg-orange-900/40 border-orange-700',
  critical: 'text-red-400 bg-red-900/40 border-red-700',
}
const RISK_STARS: Record<RiskLevel, string> = {
  low: '★☆☆☆☆', medium: '★★★☆☆', high: '★★★★☆', critical: '★★★★★',
}

const ACTION_STATUS_ICON: Record<AgentAction['status'], string> = {
  pending: '⏳',
  running: '⚡',
  success: '✓',
  failed:  '✗',
}
const ACTION_STATUS_COLOR: Record<AgentAction['status'], string> = {
  pending: 'text-slate-400',
  running: 'text-yellow-400 animate-pulse',
  success: 'text-green-400',
  failed:  'text-red-400',
}

export function AgentPanel() {
  const { agentDecision, agentLog, isSimulating } = useAppStore()

  return (
    <div className="panel flex flex-col gap-3 h-full overflow-hidden">
      <h2 className="text-sm font-bold text-slate-300 tracking-wider uppercase shrink-0">
        AI Agent 決策中心
      </h2>

      {/* Agent status badges */}
      <div className="flex gap-2 shrink-0 flex-wrap">
        {[
          { name: 'Supervisor', active: isSimulating },
          { name: 'Traffic',    active: isSimulating && !!agentDecision },
          { name: 'Medical',    active: isSimulating && !!agentDecision },
          { name: 'Disaster',   active: isSimulating && !!agentDecision },
        ].map(({ name, active }) => (
          <span
            key={name}
            className={`badge border ${active ? 'text-sky-300 bg-sky-900/40 border-sky-700' : 'text-slate-500 bg-slate-800 border-slate-700'}`}
          >
            {active ? '●' : '○'} {name}
          </span>
        ))}
      </div>

      {/* Decision card */}
      {agentDecision ? (
        <div className="flex-1 overflow-y-auto space-y-3 min-h-0">
          {/* Risk level */}
          <div className={`border rounded-lg p-3 ${RISK_COLOR[agentDecision.riskLevel]}`}>
            <div className="flex items-center justify-between mb-1">
              <span className="font-bold text-sm uppercase tracking-wide">{agentDecision.agentName}</span>
              <span className="text-xs">{RISK_STARS[agentDecision.riskLevel]} {agentDecision.riskLevel.toUpperCase()}</span>
            </div>
            <p className="text-xs leading-relaxed opacity-90">{agentDecision.decision}</p>
          </div>

          {/* Actions */}
          <div>
            <p className="text-xs text-slate-500 uppercase tracking-wider mb-2">Actions</p>
            <div className="space-y-1.5">
              {agentDecision.actions.map((action, i) => (
                <div
                  key={i}
                  className="flex items-start gap-2 bg-slate-800/60 rounded p-2 text-xs"
                >
                  <span className={`shrink-0 font-bold ${ACTION_STATUS_COLOR[action.status]}`}>
                    {ACTION_STATUS_ICON[action.status]}
                  </span>
                  <div className="flex-1 min-w-0">
                    <p className="text-slate-200 leading-tight">{action.description}</p>
                    {action.api && (
                      <p className="text-sky-500 font-mono text-[10px] mt-0.5 truncate">
                        {action.api}
                        {action.httpStatus ? ` → ${action.httpStatus}` : ''}
                      </p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Score */}
          <div className="bg-slate-800/60 rounded p-2">
            <p className="text-xs text-slate-400 mb-1">Response Score</p>
            <div className="flex items-center gap-2">
              <div className="flex-1 bg-slate-700 rounded-full h-2">
                <div
                  className="bg-green-500 h-2 rounded-full transition-all duration-1000"
                  style={{ width: `${agentDecision.score}%` }}
                />
              </div>
              <span className="text-green-400 font-bold text-sm">{agentDecision.score}/100</span>
            </div>
          </div>

          {/* Expected outcome */}
          <div className="text-xs text-slate-400 bg-slate-800/40 rounded p-2 leading-relaxed">
            <span className="text-slate-500">Expected: </span>
            {agentDecision.expectedOutcome}
          </div>
        </div>
      ) : (
        <div className="flex-1 flex items-center justify-center text-slate-600 text-sm">
          {isSimulating ? (
            <span className="animate-pulse text-sky-500">Bedrock Agent analyzing...</span>
          ) : (
            'Trigger a city event to start AI analysis'
          )}
        </div>
      )}

      {/* Log */}
      <div className="shrink-0 h-24 overflow-y-auto bg-black/40 rounded p-2 font-mono text-[10px] text-slate-500 space-y-0.5">
        {agentLog.slice(-20).map((line, i) => (
          <div key={i}>{line}</div>
        ))}
        {agentLog.length === 0 && <div className="text-slate-700">— no log —</div>}
      </div>
    </div>
  )
}
