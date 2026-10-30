import { useEffect } from 'react';
import { getFree5gcStatus, getMetrics, getRuntimeStatus } from './services/api';
import { connectWebSocket } from './services/websocket';
import { useAppStore } from './store/appStore';
import { LearningLab } from './components/LearningLab/LearningLab';
import { OnpremScenarioProvider } from './onprem/components/OnpremRuntime/ScenarioContext';
import { CityBootstrapGate } from './onprem/components/OnpremRuntime/CityBootstrapGate';
import { IntentWorkspace } from './onprem/components/IntentWorkspace/IntentWorkspace';
import { CityDemandProvider } from './city-map/context/CityDemandContext';
import { FormalIntentProvider } from './city-map/context/FormalIntentContext';
export default function App() {
    const wsConnected = useAppStore((state) => state.wsConnected);
    const runtimeBusy = useAppStore((state) => state.runtimeBusy);
    useEffect(() => {
        let cleanup: (() => void) | undefined;
        try {
            cleanup = connectWebSocket();
        }
        catch (error) {
            useAppStore.getState().appendAgentLog(`[WebSocket] connection start failed: ${String(error)}`);
        }
        return cleanup;
    }, []);
    useEffect(() => {
        let stopped = false;
        let failureCount = 0;
        const store = useAppStore.getState();
        async function syncFree5gcStatus() {
            void getRuntimeStatus()
                .then((status) => {
                if (!stopped)
                    store.setRuntimeStatus(status);
            })
                .catch(() => undefined);
            try {
                const [status, metrics] = await Promise.all([
                    getFree5gcStatus({ includeDaily: false }), getMetrics(),
                ]);
                failureCount = 0;
                if (!stopped) {
                    store.setFree5gcStatus(status);
                    store.setRuntimeBusy(false);
                    if (status.connected !== false) {
                        store.updateMetrics(metrics);
                    }
                }
            }
            catch (err) {
                if (String(err).includes('SESSION_BUSY'))
                    store.setRuntimeBusy(true);
                failureCount += 1;
                if (!stopped && failureCount >= 3) {
                    store.appendAgentLog(`[free5GC] status sync degraded after ${failureCount} attempts: ${String(err)}`);
                }
            }
        }
        syncFree5gcStatus();
        const timer = window.setInterval(syncFree5gcStatus, wsConnected ? 60000 : 15000);
        return () => {
            stopped = true;
            window.clearInterval(timer);
        };
    }, [wsConnected]);
    return <CityDemandProvider><FormalIntentProvider><div className="city-app-shell">
    <CityBootstrapGate><OnpremScenarioProvider>
      <LearningLab runtimeBusy={runtimeBusy}/>
    </OnpremScenarioProvider></CityBootstrapGate>
    <IntentWorkspace />
  </div></FormalIntentProvider></CityDemandProvider>;
}
