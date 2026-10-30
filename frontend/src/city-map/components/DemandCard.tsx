import { useEffect, useRef, useState } from 'react';
import type { ScenarioGroup } from '../../onprem/services/onpremScenarios';
import { validServiceRequirements, type ServiceRequirement } from '../../onprem/services/serviceRequirements';
import { useCityDemand } from '../context/CityDemandContext';
import { demandNames, type CityDemand } from '../domain/demand';
import { SERVICE_PROFILES } from '../../domain/scenarios';
export function DemandCard({ row, onClose }: {
    row: CityDemand;
    onClose: () => void;
}) {
    const city = useCityDemand()!;
    const buildingDemands = row.sourceLocation.buildingId
        ? city.rows.filter(item => item.sourceLocation.buildingId === row.sourceLocation.buildingId) : [];
    const [event, setEvent] = useState(row.profileId);
    const groupId = row.sourceLocation.buildingId ? `building:${row.sourceLocation.buildingId}:${event}` : row.groupId;
    const [count, setCount] = useState(String(row.ueCount));
    const [error, setError] = useState('');
    const [saved, setSaved] = useState(false);
    const shared = city.services[event];
    const [ul, setUl] = useState(String(shared?.traffic.offeredUplinkMbpsPerUe ?? ''));
    const [dl, setDl] = useState(String(shared?.traffic.offeredDownlinkMbpsPerUe ?? ''));
    const [rtt, setRtt] = useState(String(shared?.sla?.maxMeanRttMs ?? ''));
    const [minDl, setMinDl] = useState(String(shared?.sla?.minDownlinkMbps ?? ''));
    const initializedDefaults = useRef(Boolean(shared));
    const touched = useRef({ ul: false, dl: false, rtt: false, minDl: false });
    useEffect(() => {
        if (initializedDefaults.current || !shared) return;
        initializedDefaults.current = true;
        if (!touched.current.ul) setUl(String(shared.traffic.offeredUplinkMbpsPerUe));
        if (!touched.current.dl) setDl(String(shared.traffic.offeredDownlinkMbpsPerUe));
        if (!touched.current.rtt) setRtt(String(shared.sla?.maxMeanRttMs ?? ''));
        if (!touched.current.minDl) setMinDl(String(shared.sla?.minDownlinkMbps ?? ''));
    }, [shared]);
    const close = useRef<HTMLButtonElement>(null);
    useEffect(() => { close.current?.focus({ preventScroll: true }); const key = (event: KeyboardEvent) => { if (event.key === 'Escape')
        onClose(); }; window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key); }, [onClose]);
    function switchEvent(value: ScenarioGroup) {
        setEvent(value);
        setSaved(false);
        const existing = city.rows.find(item => item.sourceLocation.buildingId === row.sourceLocation.buildingId && item.profileId === value);
        setCount(String(existing?.ueCount ?? 1));
        const service = city.services[value];
        initializedDefaults.current = Boolean(service);
        touched.current = { ul: false, dl: false, rtt: false, minDl: false };
        setUl(String(service?.traffic.offeredUplinkMbpsPerUe ?? ''));
        setDl(String(service?.traffic.offeredDownlinkMbpsPerUe ?? ''));
        setRtt(String(service?.sla?.maxMeanRttMs ?? ''));
        setMinDl(String(service?.sla?.minDownlinkMbps ?? ''));
    }
    function save() {
        const ueCount = Number(count), upload = Number(ul), download = Number(dl);
        const cap = city.caps?.maxOfferedKbpsPerUePerDirection;
        const service: ServiceRequirement = { traffic: { protocol: 'udp', offeredUplinkMbpsPerUe: upload, offeredDownlinkMbpsPerUe: download },
            ...(rtt.trim() || minDl.trim() ? { sla: { scope: 'per-ue' as const,
                    ...(rtt.trim() ? { maxMeanRttMs: Number(rtt) } : {}), ...(minDl.trim() ? { minDownlinkMbps: Number(minDl) } : {}) },
                evaluation: { windowSeconds: 5, sampleIntervalSeconds: 1, minTimeCompliance: .95, minUeCompliance: 1 } } : {}) };
        const events = { video_streaming: 0,  connected_traffic: 0, realtime_control: 0, factory_iot: 0, [event]: ueCount };
        if (!Number.isSafeInteger(ueCount) || ueCount < 1 || ueCount > 100 || !ul.trim() || !dl.trim() || cap == null
            || upload * 1000 > cap || download * 1000 > cap
            || !validServiceRequirements({ schemaVersion: 'cityverse.service-requirements/v2', services: { [event]: service } }, { baselineUes: event === 'web_browsing' ? ueCount : 0, events })) {
            setError('請輸入有效的 UE 數、速率與 SLA，並遵守每方向流量上限。');
            return;
        }
        city.save({ ...row, groupId, profileId: event, ueCount }, service);
        setError('');
        setSaved(true);
    }
    return <section className="city-infra-card city-demand-card" role="dialog" aria-label={'建築與事件需求'} data-testid="city-demand-card">
    <header className="city-infra-header"><div><p className="city-eyebrow">{row.temporary ? 'TEMPORARY EVENT' : 'BUILDING DEMAND'}</p><h2>{row.label}</h2></div><button ref={close} type="button" onClick={onClose} aria-label={'關閉需求卡'}>×</button></header>
    <div className="city-infra-content">
      <p className="city-infra-note">({row.sourceLocation.position.x.toFixed(2)}, {row.sourceLocation.position.y.toFixed(2)}) km · {'下一輪草稿，尚未啟動 UE'}</p>
      <form onSubmit={e => { e.preventDefault(); save(); }} onChange={() => setSaved(false)}>
        <label className="city-field">{'網路使用行為'}<select aria-label="網路使用行為" value={event} onChange={e => switchEvent(e.target.value as ScenarioGroup)}>{Object.entries(demandNames).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select></label>
        <p className="city-infra-note">{SERVICE_PROFILES.find(profile => profile.id === event)?.description} · UDP 負載近似，非真實應用程式。</p>
        {buildingDemands.length > 0 && <div className="city-infra-note">{buildingDemands.map(item => <button key={item.groupId} type="button" onClick={() => switchEvent(item.profileId)}>{demandNames[item.profileId]} · {item.ueCount} UE</button>)}</div>}
        <label className="city-field">UE<input aria-label="需求 UE 數量" type="number" min="1" max="100" step="1" required value={count} onChange={e => setCount(e.target.value)}/></label>
        <p className="city-infra-note">{'以下速率與 SLA 由同類需求共用；儲存會同步更新同類建築與事件。'}</p>
        <div className="city-fields-grid">
          <label className="city-field">UL Mbps / UE<input aria-label="每 UE 上傳 Mbps" type="number" min="0" step="0.001" required value={ul} onChange={e => { touched.current.ul = true; setUl(e.target.value); }}/></label>
          <label className="city-field">DL Mbps / UE<input aria-label="每 UE 下載 Mbps" type="number" min="0" step="0.001" required value={dl} onChange={e => { touched.current.dl = true; setDl(e.target.value); }}/></label>
          <label className="city-field">{'最低 DL Mbps（選填）'}<input aria-label="最低下載 Mbps" type="number" min="0" step="0.001" value={minDl} onChange={e => { touched.current.minDl = true; setMinDl(e.target.value); }}/></label>
          <label className="city-field">{'最高平均 RTT ms（選填）'}<input aria-label="最高平均 RTT ms" type="number" min="0" step="0.1" value={rtt} onChange={e => { touched.current.rtt = true; setRtt(e.target.value); }}/></label>
        </div>
        <p className="city-infra-note">{'每 UE 每方向上限'}: {city.caps ? city.caps.maxOfferedKbpsPerUePerDirection / 1000 : '—'} Mbps。{'SLA 以 5 秒窗口、每秒取樣評估；時間達標 95%、UE 達標 100%。RTT 目前為相鄰探測。'}</p>
        <button className="city-wide" type="submit" disabled={!city.caps}>{'儲存需求'}</button>
        {city.rows.some(item => item.groupId === groupId) && <button className="city-wide city-danger" type="button" onClick={() => { city.remove(groupId); onClose(); }}>{'移除此需求'}</button>}
      </form>
      {saved && <p role="status" className="city-help">{'已儲存至草稿；請於右側確認需求與開始實驗。'}</p>}
      {error && <p role="alert" className="city-error">{error}</p>}
    </div>
  </section>;
}
