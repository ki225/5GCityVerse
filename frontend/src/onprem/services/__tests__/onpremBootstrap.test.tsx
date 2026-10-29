import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { CityBootstrapGate, retryableFailureStartedAt } from '../../components/OnpremRuntime/CityBootstrapGate';
import { BootstrapRequestError, nativeDeviceAdmissionReady, operatorCalibrationViewAllowed, operatorZeroResidentViewAllowed, requestBootstrapStatus, type BootstrapStatus } from '../onpremBootstrap';
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
beforeEach(() => { vi.stubEnv('VITE_API_URL', 'http://localhost:8000'); });
const absent = { state: 'absent', stage: 'ue', message: 'Citizen baseline is absent', retryable: true };
describe('on-prem city bootstrap', () => {
    it('admits a fresh explicitly empty V2 fleet without claiming existing UE readiness', () => {
        const now = Date.now();
        const status: BootstrapStatus = { state: 'absent', stage: 'ue', message: 'No fleet', retryable: true,
            executionAdmission: { schemaVersion: 'cityverse.native-execution-admission/v1',
                mode: 'baseline-device-reservation', state: 'ready_to_start', generation: 2,
                observedAt: new Date(now).toISOString(), validUntil: new Date(now + 5000).toISOString(),
                coreRanReady: true, nativeFleetEmpty: true, profileValid: false,
                profileRequired: false, launchContractReady: true,
                existingUePduReady: false, serviceEffectVerified: false } };
        expect(nativeDeviceAdmissionReady(status, now)).toBe(true);
        expect(status.state).toBe('absent');
        expect(nativeDeviceAdmissionReady(status, now + 5000)).toBe(false);
        expect(nativeDeviceAdmissionReady({ ...status, state: 'failed' }, now)).toBe(false);
        for (const field of ['coreRanReady', 'nativeFleetEmpty', 'launchContractReady'] as const)
            expect(nativeDeviceAdmissionReady({ ...status, executionAdmission: { ...status.executionAdmission!, [field]: false } }, now)).toBe(false);
        expect(nativeDeviceAdmissionReady({ ...status, executionAdmission: { ...status.executionAdmission!, validUntil: new Date(now + 60000).toISOString() } }, now)).toBe(false);
        const legacy = { ...status.executionAdmission! };
        delete legacy.profileRequired;
        delete legacy.launchContractReady;
        expect(nativeDeviceAdmissionReady({ ...status, executionAdmission: legacy }, now)).toBe(false);
    });
    it('allows only explicit, clean 100 UE operator commissioning observation', async () => {
        const round = { stage: 'completed', cleanupStatus: 'verified', capabilities: {
                start: true, commissioningEnabled: true, candidateMaxUes: 100, admissionMaxUes: 100,
                verifiedMaxUes: 0, staticPolicy: false, aiPolicy: false,
            }, releaseProof: { trafficStopped: true, policiesAbsent: true, rulesRestored: true,
                uesReleased: true, podsAbsent: true, globalSmfInventoryComplete: true,
                scope: 'operator-known-round-and-managed-runtime' } };
        const slices = Object.fromEntries(['embb', 'urllc', 'mmtc', 'v2x'].map(name => [name, { available: true, ngapAssociated: true, ueCount: 0 }]));
        const ue = { status: 'stopped', requested: 0, registered: 0, pduEstablished: 0,
            trafficVerified: 0, errors: [], ranHealth: { connected: true, slices } };
        const fetchMock = vi.fn().mockImplementation(async (url: string) => ({ ok: true,
            json: async () => url.includes('scenario-rounds') ? round : ue }));
        vi.stubGlobal('fetch', fetchMock);
        expect(await operatorCalibrationViewAllowed(absent as any)).toBe(true);
        expect(fetchMock).toHaveBeenCalledTimes(2);
        expect(await operatorCalibrationViewAllowed({ ...absent, stage: 'core' } as any)).toBe(false);
        expect(fetchMock).toHaveBeenCalledTimes(2);
        round.capabilities.verifiedMaxUes = 1;
        expect(await operatorCalibrationViewAllowed(absent as any)).toBe(false);
        round.capabilities.verifiedMaxUes = 0;
        round.capabilities.aiPolicy = true;
        expect(await operatorCalibrationViewAllowed(absent as any)).toBe(false);
        round.capabilities.aiPolicy = false;
        ue.ranHealth.slices.embb.ueCount = 1;
        expect(await operatorCalibrationViewAllowed(absent as any)).toBe(false);
        ue.ranHealth.slices.embb.ueCount = 0;
        round.releaseProof.podsAbsent = false;
        expect(await operatorCalibrationViewAllowed(absent as any)).toBe(false);
    });
    it('allows zero-resident AI100 view only with verified AI capacity and no commissioning', async () => {
        const round = { stage: 'completed', cleanupStatus: 'verified', capabilities: {
                start: true, commissioningEnabled: false, candidateMaxUes: null, admissionMaxUes: 100,
                verifiedMaxUes: 100, staticPolicy: false, aiPolicy: true, capacityStatus: 'receipt_verified',
            }, releaseProof: { trafficStopped: true, policiesAbsent: true, rulesRestored: true,
                uesReleased: true, podsAbsent: true, globalSmfInventoryComplete: true,
                scope: 'known-round-and-managed-runtime' } };
        const slices = Object.fromEntries(['embb', 'urllc', 'mmtc', 'v2x'].map(name => [name, { available: true, ngapAssociated: true, ueCount: 0 }]));
        const ue = { status: 'stopped', requested: 0, registered: 0, pduEstablished: 0,
            trafficVerified: 0, errors: [], ranHealth: { connected: true, slices } };
        const fetchMock = vi.fn().mockImplementation(async (url: string) => ({ ok: true,
            json: async () => url.includes('scenario-rounds') ? round : ue }));
        vi.stubGlobal('fetch', fetchMock);
        expect(await operatorZeroResidentViewAllowed(absent as any, 'ai100')).toBe(true);
        expect(fetchMock).toHaveBeenCalledWith('http://localhost:8000/api/onprem/scenario-rounds/current?view=summary', expect.objectContaining({ method: 'GET' }));
        expect(await operatorZeroResidentViewAllowed(absent as any, 'calibration')).toBe(false);
        round.capabilities.verifiedMaxUes = 1;
        expect(await operatorZeroResidentViewAllowed(absent as any, 'ai100')).toBe(false);
        round.capabilities.verifiedMaxUes = 100;
        round.capabilities.aiPolicy = false;
        expect(await operatorZeroResidentViewAllowed(absent as any, 'ai100')).toBe(false);
        round.capabilities.aiPolicy = true;
        round.releaseProof.podsAbsent = false;
        expect(await operatorZeroResidentViewAllowed(absent as any, 'ai100')).toBe(false);
        expect(fetchMock.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true);
    });
    it('checks without mutating and starts only through the explicit POST', async () => {
        const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => absent });
        vi.stubGlobal('fetch', fetchMock);
        expect(await requestBootstrapStatus()).toEqual(absent);
        expect(fetchMock).toHaveBeenNthCalledWith(1, 'http://localhost:8000/api/onprem/bootstrap', expect.objectContaining({ method: 'GET' }));
        await requestBootstrapStatus('POST');
        expect(fetchMock).toHaveBeenNthCalledWith(2, 'http://localhost:8000/api/onprem/bootstrap/start', expect.objectContaining({ method: 'POST' }));
    });
    it.each(['round-resources-only', 'pre-established-round-resources-only'])('admits an AI trial after independent zero retirement of a retained fleet: %s', async (scope) => {
        const round = { generation: 40, stage: 'completed', cleanupStatus: 'verified', capabilities: {
                start: true, commissioningEnabled: false, candidateMaxUes: 100, admissionMaxUes: 100,
                verifiedMaxUes: 0, staticPolicy: false, aiPolicy: true, capacityStatus: 'unverified_ai_trial',
            }, releaseProof: { trafficStopped: true, policiesAbsent: true, rulesRestored: true,
                ueLeaseReleased: true, fleetPreserved: true, uesReleased: false, podsAbsent: false,
                scope } };
        const slices = Object.fromEntries(['embb', 'urllc', 'mmtc', 'v2x'].map(name => [name, { available: true, ngapAssociated: true, ueCount: 0 }]));
        const ue = { generation: 41, observedAt: new Date().toISOString(), status: 'stopped',
            requested: 0, allocated: 0, registered: 0, pduEstablished: 0, failed: 0,
            trafficVerified: 0, perUe: [], errors: [], ranHealth: { connected: true, slices } };
        vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) => ({ ok: true,
            json: async () => url.includes('scenario-rounds') ? round : ue })));
        expect(await operatorZeroResidentViewAllowed(absent as any, 'ai100')).toBe(true);
        expect(await operatorZeroResidentViewAllowed(absent as any, 'calibration')).toBe(false);
        ue.generation = 40;
        expect(await operatorZeroResidentViewAllowed(absent as any, 'ai100')).toBe(false);
        ue.generation = 41;
        ue.observedAt = new Date(Date.now() - 20000).toISOString();
        expect(await operatorZeroResidentViewAllowed(absent as any, 'ai100')).toBe(false);
        ue.observedAt = new Date().toISOString();
        ue.ranHealth.slices.embb.ueCount = 1;
        expect(await operatorZeroResidentViewAllowed(absent as any, 'ai100')).toBe(false);
        ue.ranHealth.slices.embb.ueCount = 0;
        round.releaseProof.rulesRestored = false;
        expect(await operatorZeroResidentViewAllowed(absent as any, 'ai100')).toBe(false);
    });
    it('preserves backend stage and recovery detail on non-2xx responses', async () => {
        const failed = { state: 'failed', stage: 'core', message: 'Core cannot be provisioned', retryable: false, error: { code: 'CORE_PROVISIONING_NOT_CONFIGURED', detail: 'Configure a pinned manifest first', retryable: false } };
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => failed }));
        await expect(requestBootstrapStatus('POST')).rejects.toMatchObject({ status: failed, message: failed.error.detail });
    });
    it('rejects malformed payloads instead of claiming the city is ready', async () => {
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ state: 'ready' }) }));
        await expect(requestBootstrapStatus()).rejects.toBeInstanceOf(BootstrapRequestError);
    });
    it('opens the city while checking the network without claiming execution is ready', () => {
        const html = renderToStaticMarkup(<CityBootstrapGate><p>City dashboard</p></CityBootstrapGate>);
        expect(html).toContain('正在確認網路狀態');
        expect(html).toContain('City dashboard');
        expect(html).not.toContain("進行中");
        const zhHtml = renderToStaticMarkup(<CityBootstrapGate><p>City dashboard</p></CityBootstrapGate>);
        expect(zhHtml).toContain('網路就緒前不會啟動 UE 或 AI');
        expect(zhHtml).toContain('查看網路初始化狀態');
    });
    it('continues polling a temporary failed bootstrap for a bounded window', () => {
        const transient = { state: 'failed', stage: 'ue', message: 'UE verification unavailable', retryable: true } as const;
        const startedAt = retryableFailureStartedAt(transient, null, 1000);
        expect(startedAt).toBe(1000);
        expect(retryableFailureStartedAt(transient, startedAt, 180999)).toBe(1000);
        expect(retryableFailureStartedAt(transient, startedAt, 181000)).toBeNull();
        expect(retryableFailureStartedAt({ ...transient, retryable: false }, null, 1000)).toBeNull();
        expect(retryableFailureStartedAt({ ...transient, state: 'ready' }, null, 1000)).toBeNull();
    });
});
