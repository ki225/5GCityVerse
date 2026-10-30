import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { readServiceCapabilities, type ServiceCapabilities } from '../../onprem/services/serviceRequirements'
import type { Point } from '../domain/model'
import { createCityPreset } from '../data/presets'
import { compileCityDemand, type CityDemand, type ServiceSettings } from '../domain/demand'

function useDemandState() {
  const [{ map }] = useState(createCityPreset)
  const [rows, setRows] = useState<CityDemand[]>([])
  const [services, setServices] = useState<ServiceSettings>({})
  const [caps, setCaps] = useState<ServiceCapabilities | null>(null)
  const [catalogError, setCatalogError] = useState('')
  const [retry, setRetry] = useState(0)
  const [focus, setFocus] = useState<{ id: string; serial: number } | null>(null)
  const [eventPoint, setEventPoint] = useState<Point | null>(null)
  const [placingEvent, setPlacingEvent] = useState(false)
  useEffect(() => {
    const abort = new AbortController()
    setCaps(null); setCatalogError('')
    void readServiceCapabilities(abort.signal).then(value => {
      const city = value.cityAccessCatalog
      if (!city || city.source !== 'operator-configuration' || city.coordinateUnit !== 'km'
        || !Number.isSafeInteger(city.topologyRevision) || city.topologyRevision < 1 || typeof city.spatialRevision !== 'string') {
        throw new Error('城市位置配置尚未就緒 / City access catalog unavailable')
      }
      if (abort.signal.aborted) return
      setCaps(value)
      setServices(previous => {
        const next = { ...previous }
        for (const [key, traffic] of Object.entries(value.trafficDefaults)) {
          const group = key as keyof ServiceSettings
          next[group] ??= { traffic: { protocol: 'udp', offeredUplinkMbpsPerUe: traffic.offeredUplinkMbpsPerUe, offeredDownlinkMbpsPerUe: traffic.offeredDownlinkMbpsPerUe } }
        }
        return next
      })
    }).catch(error => { if (!abort.signal.aborted) setCatalogError(String(error.message)) })
    return () => abort.abort()
  }, [retry])
  const compiled = useMemo(() => {
    try {
      if (!caps?.cityAccessCatalog) return { request: null, error: catalogError || '等待城市服務設定 / Waiting for city configuration' }
      return { request: compileCityDemand(rows, services, caps.cityAccessCatalog.topologyRevision), error: '' }
    } catch (error) { return { request: null, error: String((error as Error).message) } }
  }, [rows, services, caps, catalogError])
  return { map, rows, services, caps, catalogError, compiled, focus, eventPoint, placingEvent,
    refreshCatalog: () => setRetry(value => value + 1),
    focusDemand: (id: string) => setFocus(current => ({ id, serial: (current?.serial ?? 0) + 1 })),
    setEventPoint, setPlacingEvent,
    save: (row: CityDemand, service: NonNullable<ServiceSettings[keyof ServiceSettings]>) => {
      setRows(current => [...current.filter(item => item.groupId !== row.groupId), structuredClone(row)])
      setServices(current => ({ ...current, [row.profileId]: structuredClone(service) }))
    },
    remove: (id: string) => setRows(current => current.filter(item => item.groupId !== id)),
  }
}
type CityDemandState = ReturnType<typeof useDemandState>
const Context = createContext<CityDemandState | null>(null)
export const useCityDemand = () => useContext(Context)
export function CityDemandProvider({ children }: { children: ReactNode }) {
  return <Context.Provider value={useDemandState()}>{children}</Context.Provider>
}
