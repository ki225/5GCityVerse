import { useEffect, useRef, useState } from 'react';
import type { DataCenter, Gnb } from '../domain/model';
import { infraConfig } from '../services/infra';
import type { InfraPod, InfraSnapshot, InfraSource } from '../services/infra';
const tiers: Record<string, string> = { edge: 'Edge / 區域邊緣', distributed: 'Distributed / 都會匯聚', central: 'Central Cloud / 中心雲', ran: 'gNB / 無線接入' };
export function InfraCard({ target, source, onClose }: {
    target: DataCenter | Gnb;
    source?: InfraSource;
    onClose: () => void;
}) {
    const [snapshot, setSnapshot] = useState<InfraSnapshot | null>(null);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);
    const [refresh, setRefresh] = useState(0);
    const [tab, setTab] = useState<'live' | 'config'>(source ? 'live' : 'config');
    const close = useRef<HTMLButtonElement>(null);
    const callback = useRef(onClose);
    callback.current = onClose;
    const site = infraConfig.sites.find(s => s.id === target.id);
    const tier = site?.tier ?? ('tier' in target ? target.tier : 'ran');
    const configured = infraConfig.workloads.filter(w => w.siteId === target.id);
    const pods = snapshot?.pods.filter(p => p.siteId === target.id) ?? [];
    useEffect(() => {
        close.current?.focus({ preventScroll: true });
        const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') {
            event.preventDefault();
            callback.current();
        } };
        document.addEventListener('keydown', escape);
        return () => document.removeEventListener('keydown', escape);
    }, []);
    useEffect(() => {
        if (!source || tab !== 'live')
            return;
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 12000);
        let active = true;
        setLoading(true);
        setError('');
        setSnapshot(null);
        source.read(controller.signal).then(data => { if (active)
            setSnapshot(data); })
            .catch(reason => { if (active)
            setError(controller.signal.aborted ? '讀取逾時，請稍後更新' : reason instanceof Error ? reason.message : '無法讀取 Pod 資訊'); })
            .finally(() => { clearTimeout(timeout); if (active)
            setLoading(false); });
        return () => { active = false; clearTimeout(timeout); controller.abort(); };
    }, [source, target.id, refresh, tab]);
    return <section className="city-infra-card" aria-label="Infra 資訊卡" data-testid="infra-card" data-site-id={target.id}>
    <header className="city-infra-header"><div><p className="city-eyebrow">INFRASTRUCTURE</p><h2>{target.label}</h2><p className="city-infra-tier">{tiers[tier] ?? tier}</p></div><button ref={close} onClick={onClose} aria-label="關閉 Infra 資訊卡">×</button></header>
    <div className="city-infra-meta"><code>{target.id}</code><span>地圖 ({target.position.x.toFixed(1)}, {target.position.y.toFixed(1)}) km</span>{site?.relativeCapacity && <span>相對規模 {site.relativeCapacity}×</span>}</div>
    <div className="city-infra-tabs" aria-label="資料來源"><button aria-pressed={tab === 'live'} disabled={!source} onClick={() => setTab('live')}>即時 Pod</button><button aria-pressed={tab === 'config'} onClick={() => setTab('config')}>配置定義</button>{tab === 'live' && <button className="city-infra-refresh" onClick={() => setRefresh(n => n + 1)} disabled={loading}>更新</button>}</div>
    <div className="city-infra-content" aria-live="polite" aria-busy={loading}>
      {tab === 'live' ? <>
        {loading && <p className="city-infra-note">正在讀取 Kubernetes…</p>}
        {error && <div role="alert" className="city-infra-warning">{error}<p>可切換「配置定義」查看設定；即時狀態尚未確認。</p></div>}
        {snapshot && <><div className="city-infra-status"><strong>{pods.length} Pods</strong><span>{pods.filter(p => p.ready && p.phase === 'Running').length} Ready</span><span>{snapshot.namespace}</span></div><p className="city-infra-note">觀測於 {new Date(snapshot.observedAt).toLocaleString('zh-TW')} · 手動更新</p>
          {pods.length ? pods.map(pod => <PodResource key={pod.uid} pod={pod}/>) : <p className="city-infra-empty">目前未觀測到此站點的 NF Pod。{tier === 'edge' ? '此 Edge 為預留站點。' : ''}</p>}
          <p className="city-infra-note">CPU／記憶體顯示 Pod 規格的 requests／limits。即時使用量尚未提供。</p>
        </>}
      </> : <>
        <p className="city-infra-note">來自 infra 配置定義，非目前執行狀態。地圖移動不會搬遷 Pod。</p>
        {configured.length ? configured.map(w => <article className="city-infra-pod" key={w.workload}><div className="city-infra-pod-title"><strong>{w.function}</strong><span className="city-infra-badge">配置</span></div><code>{w.workload}</code><ResourceTable requests={w.resources.requests} limits={w.resources.limits}/></article>) : <p className="city-infra-empty">預留站點，尚未配置 NF Pod。</p>}
      </>}
    </div>
  </section>;
}
function PodResource({ pod }: {
    pod: InfraPod;
}) {
    return <article className="city-infra-pod"><div className="city-infra-pod-title"><strong>{pod.function}</strong><span className={`city-infra-badge ${pod.ready && pod.phase === 'Running' ? 'is-ready' : ''}`}>{pod.phase} · {pod.ready ? 'Ready' : '未 Ready'}</span></div><code>{pod.name}</code><dl className="city-infra-pod-meta"><div><dt>Node</dt><dd>{pod.node ?? '未排程'}</dd></div><div><dt>Pod IP</dt><dd>{pod.podIp ?? '尚未分配'}</dd></div></dl>
    {pod.siteBinding === 'configured-workload' && <p className="city-infra-note">站點歸屬依 workload 配置對照，Pod 未提供有效站點標記。</p>}
    {pod.containers.map(c => <div className="city-infra-container" key={c.name}><div><span>{c.name}</span><span>重啟 {c.restarts} 次</span></div><ResourceTable requests={c.requests} limits={c.limits}/><details><summary>映像版本</summary><code>{c.image ?? '未提供'}</code></details></div>)}
  </article>;
}
function ResourceTable({ requests, limits }: {
    requests: {
        cpu?: string | null;
        memory?: string | null;
    };
    limits: {
        cpu?: string | null;
        memory?: string | null;
    };
}) {
    return <table className="city-infra-resources"><caption>資源配置</caption><thead><tr><th>資源</th><th>Request</th><th>Limit</th></tr></thead><tbody><tr><th>CPU</th><td>{requests.cpu ?? '未設定'}</td><td>{limits.cpu ?? '未設定'}</td></tr><tr><th>Memory</th><td>{requests.memory ?? '未設定'}</td><td>{limits.memory ?? '未設定'}</td></tr></tbody></table>;
}
