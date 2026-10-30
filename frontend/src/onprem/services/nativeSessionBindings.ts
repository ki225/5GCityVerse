import type { ReceiverMeasurement, ScenarioSnapshot, UeBinding } from './onpremScenarios'

export const NATIVE_SESSION_FIELDS = ['sessionKey', 'deviceId', 'supi', 'pduSessionId', 'sessionId',
  'establishedEpoch', 'smContextRef', 'podUid', 'generation', 'snssai', 'dnn', 'upf', 'nativeUpfId',
  'upfEpoch', 'ueIp', 'tun', 'slice', 'containerId', 'processEpoch'] as const
type NativeBinding = UeBinding & Record<Exclude<typeof NATIVE_SESSION_FIELDS[number], 'generation' | 'pduSessionId' | 'sessionId'>, string> & {
  generation: number; pduSessionId: number; sessionId: number; pfcpSeid: string; nonceDigest: string; proof: Record<string, unknown>
}
export type NativeSessionBindings = { schemaVersion: 'cityverse.native-session-bindings/v1'; available: boolean;
  source?: string; roundId?: string; generation?: number; populationRevision?: number;
  observedAt?: string; validUntil?: string; openedAt?: number; closedAt?: number;
  openingCensusDigest?: string; closingCensusDigest?: string; deviceCount?: number; sessionCount?: number;
  cohortDigest?: string; allocationDigest?: string; heldAllocationRef?: string; bindingsDigest?: string;
  bindings?: NativeBinding[]; reason?: string }
const object = (v: unknown): v is Record<string, any> => Boolean(v && typeof v === 'object' && !Array.isArray(v))
const text = (v: unknown): v is string => typeof v === 'string' && v.trim().length > 0 && v.length <= 2048
  && !['unknown', 'none', 'null'].includes(v.toLowerCase())
const hash = (v: unknown) => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v)
const time = (v: unknown) => typeof v === 'number' ? v * 1000 : typeof v === 'string' ? Date.parse(v) : NaN
const fresh = (v: unknown, now: number) => Number.isFinite(time(v)) && now - time(v) >= -1000 && now - time(v) <= 15000
const source = 'native-free-ran-ue-single-session/v2'

/** The server validates the producer's SHA and held-nonce join; the client
 * independently checks display scope/incarnation/source clocks. This grants no writes. */
export function validatedNativeBindings(snapshot: ScenarioSnapshot, now: number): NativeBinding[] | null {
  const v: unknown = snapshot.nativeSessionBindings
  if (!object(v) || v.schemaVersion !== 'cityverse.native-session-bindings/v1' || v.available !== true
    || v.source !== 'native-free-ran-ue-typed-cohort/v1' || v.roundId !== snapshot.roundId
    || v.generation !== snapshot.generation || !Number.isSafeInteger(v.populationRevision) || v.populationRevision < 0
    || v.populationRevision !== (snapshot.populationRevision ?? 0)
    || !fresh(v.observedAt, now) || !(time(v.validUntil) > now)
    || Math.abs(time(v.validUntil) - time(v.observedAt) - 15000) > 1
    || typeof v.openedAt !== 'number' || typeof v.closedAt !== 'number' || !fresh(v.openedAt, now) || !fresh(v.closedAt, now)
    || v.openedAt > v.closedAt || time(v.closedAt) > time(v.observedAt) + 1000
    || !['openingCensusDigest', 'closingCensusDigest', 'cohortDigest', 'allocationDigest', 'heldAllocationRef', 'bindingsDigest'].every(k => hash(v[k]))
    || v.openingCensusDigest !== v.closingCensusDigest || !Array.isArray(v.bindings)
    || !Number.isSafeInteger(v.deviceCount) || v.deviceCount < 1 || v.deviceCount > 100
    || v.deviceCount !== snapshot.allocation.total || v.deviceCount !== v.sessionCount || v.bindings.length !== v.sessionCount) return null
  const supis = new Set<string>(), keys = new Set<string>(), devices = new Set<string>(), pods = new Set<string>()
  const original = new Map(snapshot.perUe.map(r => [r.supi, r]))
  if (original.size !== snapshot.allocation.total) return null
  for (const row of v.bindings) {
    if (!object(row) || !NATIVE_SESSION_FIELDS.every(k => k === 'generation' || k === 'pduSessionId' || k === 'sessionId' ? Number.isSafeInteger(row[k]) : text(row[k]))
      || !hash(row.nonceDigest) || !text(row.pfcpSeid) || row.generation !== snapshot.generation || row.sessionId !== row.pduSessionId
      || row.pduSessionId < 1 || row.pduSessionId > 15 || !original.has(row.supi)
      || original.get(row.supi)!.podUid !== row.podUid || original.get(row.supi)!.scenario !== row.scenario
      || original.get(row.supi)!.slice !== row.slice || supis.has(row.supi) || keys.has(row.sessionKey)
      || devices.has(row.deviceId) || pods.has(row.podUid)) return null
    let parts: unknown
    try { parts = JSON.parse(row.sessionKey) } catch { return null }
    if (!Array.isArray(parts) || parts.length !== 4 || JSON.stringify(parts) !== row.sessionKey
      || !text(parts[0]) || parts[1] !== row.supi || parts[2] !== row.pduSessionId || parts[3] !== row.establishedEpoch) return null
    const p = row.proof, radio = object(p) ? p.spatialRan : null
    if (!object(p) || p.source !== source || !fresh(p.observedAt, now) || p.sessionKey !== row.sessionKey
      || !['amf_registered', 'smf_active', 'pfcp_kernel_verified', 'tun_present', 'connectivity_verified'].every(k => p[k] === true)
      || !['podUid', 'containerId', 'processEpoch', 'upfEpoch'].every(k => p[k] === row[k])
      || !text(p.smfBootEpoch) || ['unknown', 'none', 'null'].includes(p.smfBootEpoch.toLowerCase())
      || !object(radio) || radio.source !== source || radio.supi !== row.supi || radio.vendor !== 'free-ran-ue'
      || radio.gnbPodUid !== p.gnbPodUid || radio.pdr_far_gnb_verified !== true || !fresh(radio.observedAt, now)
      || !object(radio.peer) || radio.peer.ngSetupState !== 'CONNECTED') return null
    supis.add(row.supi); keys.add(row.sessionKey); devices.add(row.deviceId); pods.add(row.podUid)
  }
  return v.bindings as NativeBinding[]
}

/** Each direction needs its original native receiver window, not just a fresh fence. */
export function nativeReceiverDirections(row: ReceiverMeasurement, owner: NativeBinding, now: number) {
  const r = row as ReceiverMeasurement & Record<string, any>
  const fields = [...NATIVE_SESSION_FIELDS, 'pfcpSeid'] as const
  if (!fields.every(k => r[k] === owner[k]) || !Array.isArray(r.windows)) return { ul: false, dl: false }
  const measured = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v >= 0
  const valid = (direction: 'ul' | 'dl') => {
    const windows = r.windows.filter((w: unknown) => object(w) && w.direction === direction)
    const metric = direction === 'ul' ? r.receivedUplinkMbps : r.receivedDownlinkMbps
    return windows.length === 1 && windows.every((w: any) => w.source === 'native-iperf3-receiver'
      && object(w.binding) && fields.every(k => w.binding[k] === owner[k])
      && fresh(w.finishedAt, now) && fresh(w.receiverFinishedAt, now) && measured(w.receivedMbps)
      && measured(metric) && Math.abs(metric - w.receivedMbps) <= 1e-9 * Math.max(1, w.receivedMbps))
  }
  return { ul: measured(r.receivedUplinkMbps) && valid('ul'), dl: measured(r.receivedDownlinkMbps) && valid('dl') }
}
