import catalog from '../../../shared/service-profiles.json'
export type ServiceProfileId = 'web_browsing' | 'video_streaming' | 'realtime_control' | 'connected_traffic' | 'factory_iot'
export const SERVICE_PROFILES = catalog.profiles as Array<{id:ServiceProfileId;name:string;slice:string;description:string;trafficModel:string;offeredUplinkMbpsPerUe:number;offeredDownlinkMbpsPerUe:number}>
export const SERVICE_PROFILE_IDS = SERVICE_PROFILES.map(profile => profile.id)
export const SCENARIO_EVENTS = SERVICE_PROFILE_IDS.filter((id): id is Exclude<ServiceProfileId,'web_browsing'> => id !== 'web_browsing')
export const MAX_SCENARIO_UES = 100
export type ScenarioEvent = Exclude<ServiceProfileId,'web_browsing'>
export type ScenarioGroup = ServiceProfileId
export const SCENARIO_SLICES = Object.fromEntries(SERVICE_PROFILES.map(p => [p.id,p.slice])) as Record<ScenarioGroup,string>
export const demandNames = Object.fromEntries(SERVICE_PROFILES.map(p => [p.id,p.name])) as Record<ScenarioGroup,string>
