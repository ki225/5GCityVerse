import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { scenarioErrorText, acceptScenarioSnapshot, freshTimestamp, getScenarioRound, selectedActiveScenario, type ScenarioSnapshot, type ScenarioGroup } from '../../services/onpremScenarios';
import { scenarioPollInterval, startScenarioPolling } from '../../services/scenarioPolling';
import type { AgentRole, ObservationState } from '../../services/roundObservability';
import { useRoundObservability } from './useRoundObservability';
/** Read-only telemetry. All service mutations are owned by the formal intent API. */
export type ScenarioAdapter = {
    snapshot: ScenarioSnapshot | null;
    error: string;
    pending: boolean;
    uncertain: boolean;
    now: number;
    fresh: boolean;
    blockingError?: boolean;
    observation?: ObservationState;
    selectedScenario?: ScenarioGroup;
    selectScenario?: (scenario: ScenarioGroup) => void;
    selectedSampleId?: string | null;
    selectSample?: (id: string | null) => void;
    selectedDecisionId?: string | null;
    selectDecision?: (id: string | null) => void;
    selectedActionId?: string | null;
    selectAction?: (id: string | null) => void;
    selectedAgentRole?: AgentRole | null;
    selectAgentRole?: (role: AgentRole | null) => void;
};
const ScenarioContext = createContext<ScenarioAdapter | null>(null);
export const useOnpremScenario = () => useContext(ScenarioContext);
export const ScenarioAdapterProvider = ScenarioContext.Provider;
export function OnpremScenarioProvider({ children }: {
    children: ReactNode;
}) {
    const [snapshot, setSnapshot] = useState<ScenarioSnapshot | null>(null);
    const [cause, setCause] = useState<unknown>(null);
    const error = scenarioErrorText(cause);
    const [now, setNow] = useState(Date.now());
    const observation = useRoundObservability(snapshot?.roundId ?? null, snapshot?.generation ?? null);
    const [selectedScenario, selectScenario] = useState<ScenarioGroup>('web_browsing');
    const [selectedSampleId, selectSample] = useState<string | null>(null);
    const [selectedDecisionId, selectDecision] = useState<string | null>(null);
    const [selectedActionId, selectAction] = useState<string | null>(null);
    const [selectedAgentRole, selectAgentRole] = useState<AgentRole | null>(null);
    useEffect(() => { selectSample(null); selectDecision(null); selectAction(null); selectAgentRole(null); }, [snapshot?.roundId, snapshot?.generation]);
    useEffect(() => { selectScenario(current => selectedActiveScenario(current, snapshot?.allocation)); }, [snapshot?.allocation]);
    useEffect(() => {
        const stop = startScenarioPolling(getScenarioRound, incoming => { setSnapshot(current => acceptScenarioSnapshot(current, incoming)); setCause(null); }, setCause, { intervalMs: scenarioPollInterval });
        const clock = setInterval(() => setNow(Date.now()), 1000);
        return () => { stop(); clearInterval(clock); };
    }, []);
    return <ScenarioContext.Provider value={{ snapshot, error, pending: false, uncertain: false, now, observation,
            blockingError: Boolean(error), fresh: !error && freshTimestamp(snapshot?.observedAt, now),
            selectedScenario, selectScenario: scenario => { selectScenario(scenario); selectSample(null); selectAction(null); },
            selectedSampleId, selectSample: id => { selectSample(id); selectAction(null); },
            selectedDecisionId, selectDecision: id => { selectDecision(id); selectAction(null); }, selectedActionId, selectAction,
            selectedAgentRole, selectAgentRole: role => { selectAgentRole(role); selectAction(null); },
        }}>{children}</ScenarioContext.Provider>;
}
