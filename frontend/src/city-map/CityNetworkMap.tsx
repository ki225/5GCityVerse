import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { sessionHeaders } from '../services/browserSession';
import { apiBaseUrl } from '../services/runtimeConfig';
import { CityMapCanvas } from './components/CityMapCanvas';
import { InfraCard } from './components/InfraCard';
import { HttpInfraSource, infraTarget } from './services/infra';
import { Building, DataCenter, Gnb, type CityObject } from './domain/model';
import { createCityPreset } from './data/presets';
import type { CityMapRenderer } from './rendering/renderer';
import './styles/city-map.css';
import './styles/city-network-map.css';
import './styles/city-demand.css';
import { useCityDemand } from './context/CityDemandContext';
import { DemandCard } from './components/DemandCard';
import { buildingEvent, pointInCity, type CityDemand } from './domain/demand';
import { DemandMarker, paintDemandMarker } from './rendering/demandMarkers';
import { useCityMonitor } from './context/CityMonitorContext';
import { CityObservationOverlay } from './components/CityObservationOverlay';
import { CityMonitorPanel } from './components/CityMonitorPanel';
/** The dashboard owns one stable scene; selection never rebuilds its renderer. */
export function CityNetworkMap() {
    const city = useCityDemand();
    const monitor = useCityMonitor();
    const [activeRenderer, setActiveRenderer] = useState<CityMapRenderer | null>(null);
    const [map] = useState(() => city?.map ?? createCityPreset().map);
    const renderer = useRef<CityMapRenderer | null>(null);
    const mapRegion = useRef<HTMLElement>(null);
    const [selected, setSelected] = useState<CityObject | null>(null);
    const [showLabels, setShowLabels] = useState(true);
    const [editing, setEditing] = useState<CityDemand | null>(null);
    const [placementError, setPlacementError] = useState('');
    const source = useMemo(() => new HttpInfraSource(import.meta.env.VITE_CITY_INFRA_API_URL || `${apiBaseUrl()}/api/onprem/city-infra`, sessionHeaders), []);
    const sites = useMemo(() => map.objects.filter(item => item instanceof DataCenter || item instanceof Gnb), [map]);
    const buildings = useMemo(() => map.objects.filter((item): item is Building => item instanceof Building), [map]);
    const target = infraTarget(map, selected);
    const monitorOpen = Boolean(monitor?.mode === 'monitor' && (monitor.selectedId || monitor.pathId));
    const inspectorOpen = monitorOpen || Boolean(editing || (!monitor && target));
    const closeDemand = useCallback(() => { setEditing(null); renderer.current?.select(null); mapRegion.current?.focus({ preventScroll: true }); }, []);
    function select(object: CityObject | null) {
        setSelected(object);
        monitor?.setSelectedId(object?.id ?? null);
        monitor?.setPathId(null);
        const host = infraTarget(map, object);
        if (monitor && host) {
            monitor.setSelectedId(host.id);
            monitor.setMode('monitor'); setEditing(null); return;
        }
        if (monitor?.mode === 'monitor') { setEditing(null); return; }
        if (city && object instanceof Building) {
            const existing = city.rows.find(row => row.sourceLocation.buildingId === object.id);
            setEditing(existing ?? { groupId: `building:${object.id}:${buildingEvent(object)}`, label: object.label, temporary: false,
                profileId: buildingEvent(object), ueCount: 1,
                sourceLocation: { type: 'point', buildingId: object.id, position: { ...object.position } } });
        }
        else if (object instanceof DemandMarker)
            setEditing(object.demand);
        else
            setEditing(null);
    }
    useEffect(() => {
        if (monitor?.mode === 'monitor') setEditing(null);
        else if (monitor?.selectedId) {
            const object = map.get(monitor.selectedId);
            if (object instanceof Building) select(object);
        }
    }, [monitor?.mode]);
    useEffect(() => {
        if (monitor?.mode === 'monitor' && (selected?.id ?? null) !== monitor.selectedId) {
            renderer.current?.select(monitor.selectedId ? map.get(monitor.selectedId) ?? null : null);
        }
    }, [monitor?.mode, monitor?.selectedId, selected?.id, map]);
    useEffect(() => {
        if (!city)
            return;
        // Markers are model objects; no per-UE actors or continuous rendering.
        map.batch(() => {
            for (const object of map.objects)
                if (object instanceof DemandMarker && !city.rows.some(row => row.groupId === object.demand.groupId))
                    map.remove(object.id);
            for (const row of city.rows) {
                const marker = map.get(`demand-marker:${row.groupId}`);
                if (marker instanceof DemandMarker) {
                    marker.demand = row;
                    map.changed('network');
                }
                else
                    map.add(new DemandMarker(row));
            }
        });
    }, [map, city?.rows]);
    useEffect(() => {
        if (!city?.focus)
            return;
        const row = city.rows.find(item => item.groupId === city.focus!.id);
        const object = row && map.get(row.sourceLocation.buildingId ?? `demand-marker:${row.groupId}`);
        if (object)
            renderer.current?.focus(object);
    }, [city?.focus, map]);
    return <section ref={mapRegion} tabIndex={-1} className="city-lab city-network-map" aria-labelledby="city-network-map-title" data-testid="city-network-map">
    <header className="city-board-header">
      <div><h2 id="city-network-map-title">{'城市網路地圖'}</h2>
        <p>24 × 24 km · 7 gNB · 3 Distributed · 1 Central</p></div>
      <details className="city-map-options"><summary>地圖設定</summary><label className="city-map-label-toggle"><input type="checkbox" checked={showLabels} onChange={event => setShowLabels(event.target.checked)}/>地圖文字</label></details>
    </header>
    {monitor && <div className="city-monitor-controls"><label><input type="checkbox" checked={monitor.showPaths} onChange={e => { monitor.setShowPaths(e.target.checked); if (e.target.checked) monitor.setMode('monitor'); }}/>流量路徑</label><label><input type="checkbox" checked={monitor.showHealth} onChange={e => monitor.setShowHealth(e.target.checked)}/>Infra 健康燈</label><button onClick={() => monitor.setMode(monitor.mode === 'monitor' ? 'demand' : 'monitor')}>{monitor.mode === 'monitor' ? '編輯需求' : '網路監看'}</button></div>}
    <div className="city-map-controls" hidden>
      <div className="city-map-picker"><label htmlFor="city-infra-picker">檢視設備</label>
        <select id="city-infra-picker" value={target?.id ?? ''} onChange={event => renderer.current?.select(map.get(event.target.value) ?? null)}>
          <option value="">選擇機房或基地台</option>{sites.map(site => <option key={site.id} value={site.id}>{site.label}</option>)}
        </select>
      </div>
      {city && <><div className="city-map-picker"><label htmlFor="city-building-picker">建築需求</label>
        <select id="city-building-picker" value={editing?.sourceLocation.buildingId ?? ''} onChange={event => { city.setPlacingEvent(false); const object = map.get(event.target.value); if (object) renderer.current?.focus(object); }}>
          <option value="" disabled>選擇建築</option>{buildings.map(building => <option key={building.id} value={building.id}>{building.label}</option>)}
        </select>
      </div><button type="button" className="city-event-button" aria-pressed={city.placingEvent} onClick={() => { setEditing(null); setPlacementError(''); city.setPlacingEvent(!city.placingEvent); }}>{city.placingEvent ? '取消選點' : '新增臨時事件'}</button></>}
    </div>
    {city?.placingEvent && <div className="city-placement-hint" role="status">{'點選城市內的位置以新增事件；拖曳仍可平移。也可輸入座標：'}
      <form onSubmit={event => {
                event.preventDefault();
                const data = new FormData(event.currentTarget), point = { x: Number(data.get('x')), y: Number(data.get('y')) };
                if (pointInCity(point)) {
                    city.setEventPoint(point);
                    city.setPlacingEvent(false);
                    setSelected(null);
                    setEditing({ groupId: `event:${crypto.randomUUID()}`, label: '臨時事件', temporary: true, profileId: 'realtime_control', ueCount: 1, sourceLocation: { type: 'point', position: point } });
                }
            }}><label>x km<input name="x" aria-label="事件 x km" type="number" min="0" max="24" step="0.01" required defaultValue="12"/></label><label>y km<input name="y" aria-label="事件 y km" type="number" min="0" max="24" step="0.01" required defaultValue="12"/></label><button type="submit">{'設定位置'}</button></form>
    </div>}
    {placementError && <p role="alert" className="city-placement-hint">{placementError}</p>}
    <div className="city-map-surface" data-inspector-open={inspectorOpen}>
      <CityMapCanvas map={map} showLabels={showLabels} className="city-canvas" onSelect={select} onMapClick={(point, object) => {
            if (!city?.placingEvent)
                return false;
            if (!pointInCity(point) || object?.space === 'external') {
                setPlacementError('請選擇 24 × 24 km 城市內的位置。');
                return true;
            }
            const position = { x: Math.round(point.x * 100) / 100, y: Math.round(point.y * 100) / 100 };
            city.setPlacingEvent(false);
            setPlacementError('');
            renderer.current?.select(null);
            setEditing({ groupId: `event:${crypto.randomUUID()}`, label: '臨時事件', temporary: true, profileId: 'realtime_control', ueCount: 1, sourceLocation: { type: 'point', position } });
            return true;
        }} onReady={value => { renderer.current = value; setActiveRenderer(value); value?.setLayerVisible('ue', false); value?.registerPainter('demand-marker', paintDemandMarker); }}/>
      {monitor && <CityObservationOverlay map={map} renderer={activeRenderer}/>}
      <div className="city-camera" aria-label={'地圖視角'}>
        <button type="button" onClick={() => renderer.current?.zoomBy(1.2)} aria-label={'放大地圖'}>＋</button>
        <button type="button" onClick={() => renderer.current?.zoomBy(1 / 1.2)} aria-label={'縮小地圖'}>−</button>
        <button type="button" onClick={() => renderer.current?.resetView()} aria-label={'重設地圖視角'}>↺</button>
      </div>
      <aside className="city-map-inspector" aria-label="城市元件詳情" aria-hidden={!inspectorOpen} data-testid="city-map-inspector">
      {monitorOpen && <CityMonitorPanel onClose={closeDemand}/>}
      {!monitor && target && !editing && <InfraCard key={target.id} target={target} source={source} onClose={() => { renderer.current?.select(null); mapRegion.current?.focus({ preventScroll: true }); }}/>}
      {!monitorOpen && editing && city && <DemandCard key={editing.groupId} row={editing} onClose={closeDemand}/>}
      </aside>
    </div>
    <footer className="city-legend">
      <span>{'建築・基地台・機房'}</span>
      <span>{monitor ? '流帶：服務路徑 · 光段：UL／DL 方向 · 虛線：配置／未知' : '點選設備讀取即時 Pod'}</span>
      <span>{monitor ? '健康燈表示 NF 工作負載狀態；機房位置為圖面配置。' : 'Central 位於地圖中央。'}</span>
    </footer>
  </section>;
}
