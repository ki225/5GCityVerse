import { sessionHeaders } from '../../services/browserSession'
import { apiBaseUrl } from '../../services/runtimeConfig'

export type BootstrapStage = 'core' | 'gnb' | 'ue' | 'pdu'
export type BootstrapState = 'absent' | 'starting' | 'ready' | 'failed'

export interface BootstrapStatus {
  state: BootstrapState
  stage: BootstrapStage | null
  message: string
  retryable: boolean
  error?: { code: string; detail: string; retryable: boolean }
  executionAdmission?: {
    schemaVersion: 'cityverse.native-execution-admission/v1'
    mode: 'baseline-device-reservation'
    state: 'ready_to_start' | 'unavailable'
    observedAt: string
    validUntil: string
    generation?: number
    coreRanReady: boolean
    nativeFleetEmpty: boolean
    profileValid: boolean
    profileRequired?: false
    launchContractReady?: boolean
    reasonCode?: string | null
    existingUePduReady: false
    serviceEffectVerified: false
  }
}

/** Permission to create a new UE is separate from existing UE/PDU readiness. */
export function nativeDeviceAdmissionReady(status: BootstrapStatus | null, now = Date.now()): boolean {
  const a = status?.executionAdmission
  if (status?.state !== 'absent' || status.stage !== 'ue' || !a) return false
  const observed = Date.parse(a.observedAt), until = Date.parse(a.validUntil)
  return a.schemaVersion === 'cityverse.native-execution-admission/v1'
    && a.mode === 'baseline-device-reservation' && a.state === 'ready_to_start'
    && a.coreRanReady === true && a.nativeFleetEmpty === true
    && a.profileRequired === false && a.launchContractReady === true
    && a.existingUePduReady === false && a.serviceEffectVerified === false
    && Number.isSafeInteger(a.generation) && a.generation! >= 0
    && Number.isFinite(observed) && Number.isFinite(until)
    && observed <= now + 1000 && now - observed <= 15000 && until > now && until - observed <= 5000
}

export class BootstrapRequestError extends Error {
  constructor(message: string, readonly status?: BootstrapStatus) {
    super(message)
    this.name = 'BootstrapRequestError'
  }
}

function isBootstrapStatus(value: unknown): value is BootstrapStatus {
  if (!value || typeof value !== 'object') return false
  const status = value as Record<string, unknown>
  return ['absent', 'starting', 'ready', 'failed'].includes(String(status.state))
    && (status.stage === null || ['core', 'gnb', 'ue', 'pdu'].includes(String(status.stage)))
    && typeof status.message === 'string' && typeof status.retryable === 'boolean'
}

export async function requestBootstrapStatus(method: 'GET' | 'POST' = 'GET', signal?: AbortSignal): Promise<BootstrapStatus> {
  const response = await fetch(`${apiBaseUrl()}/api/onprem/bootstrap${method === 'POST' ? '/start' : ''}`, {
    method,
    headers: sessionHeaders(),
    signal,
  })
  const payload: unknown = await response.json().catch(() => null)
  if (!isBootstrapStatus(payload)) throw new BootstrapRequestError(`Bootstrap API: invalid response (HTTP ${response.status})`)
  if (!response.ok) throw new BootstrapRequestError(payload.error?.detail || payload.message || `HTTP ${response.status}`, payload)
  return payload
}

// The operator's zero-resident calibration has no citizen UE baseline by design.
// This only grants access to the existing scenario view; round admission stays server-side.
export async function operatorCalibrationViewAllowed(bootstrap: BootstrapStatus, signal?: AbortSignal): Promise<boolean> {
  return operatorZeroResidentViewAllowed(bootstrap, 'calibration', signal)
}

export async function operatorZeroResidentViewAllowed(bootstrap: BootstrapStatus, mode: 'calibration' | 'ai100', signal?: AbortSignal): Promise<boolean> {
  if (bootstrap.state !== 'absent' || bootstrap.stage !== 'ue') return false
  try {
    // This gate needs release/capacity metadata, not raw traffic reports.
    const roundPath = '/api/onprem/scenario-rounds/current?view=summary'
    const paths = [roundPath, '/api/onprem/ue']
    const responses = await Promise.all(paths.map(path => fetch(`${apiBaseUrl()}${path}`, {
      method: 'GET', headers: sessionHeaders(), signal,
    })))
    if (responses.some(response => !response.ok)) return false
    const [round, ue] = await Promise.all(responses.map(response => response.json()))
    const caps = round?.capabilities
    const proof = round?.releaseProof
    const ran = ue?.ranHealth
    const slices = ran?.slices
    const scope = mode === 'calibration'
      ? caps?.commissioningEnabled === true && caps?.candidateMaxUes === 100
        && caps?.verifiedMaxUes === 0 && caps?.aiPolicy === false
      : caps?.commissioningEnabled === false && caps?.aiPolicy === true
        && ((caps?.candidateMaxUes == null && caps?.verifiedMaxUes === 100
             && caps?.capacityStatus === 'receipt_verified')
          || (caps?.candidateMaxUes === 100 && caps?.verifiedMaxUes === 0
              && caps?.capacityStatus === 'unverified_ai_trial'))
    const fullRelease = proof?.uesReleased === true && proof?.podsAbsent === true
      && proof?.globalSmfInventoryComplete === true
      && ['operator-known-round-and-managed-runtime', 'known-round-and-managed-runtime'].includes(proof?.scope)
    const observedAge = Date.now() - Date.parse(ue?.observedAt ?? '')
    const stoppedSuccessor = mode === 'ai100'
      && ['round-resources-only', 'pre-established-round-resources-only'].includes(proof?.scope)
      && proof?.ueLeaseReleased === true && proof?.fleetPreserved === true
      && Number.isSafeInteger(round?.generation) && Number.isSafeInteger(ue?.generation)
      && ue.generation > round.generation && ue?.allocated === 0 && ue?.failed === 0
      && Array.isArray(ue?.perUe) && ue.perUe.length === 0
      && Number.isFinite(observedAge) && observedAge >= -1000 && observedAge <= 15000
    return caps?.start === true && scope && caps?.admissionMaxUes === 100
      && caps?.staticPolicy === false
      && round?.stage === 'completed' && round?.cleanupStatus === 'verified'
      && proof?.trafficStopped === true && proof?.policiesAbsent === true
      && proof?.rulesRestored === true && (fullRelease || stoppedSuccessor)
      && ue?.status === 'stopped' && ue?.requested === 0 && ue?.registered === 0
      && ue?.pduEstablished === 0 && ue?.trafficVerified === 0
      && Array.isArray(ue?.errors) && ue.errors.length === 0
      && ran?.connected === true && ['embb', 'urllc', 'mmtc', 'v2x'].every(name =>
        slices?.[name]?.available === true && slices[name].ngapAssociated === true && slices[name].ueCount === 0)
  } catch {
    return false
  }
}
