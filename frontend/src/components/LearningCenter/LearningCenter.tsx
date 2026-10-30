import { useMemo, useState } from 'react';
import { useAppStore } from '../../store/appStore';
type Props = {
    onClose: () => void;
};
const NFS = [
    { id: 'AMF', icon: '🚪', title: '核網的大門與交通警察', job: '接住基地台送來的訊號，管理 UE 註冊、連線與移動。', spec: 'N1 / N2 · TS 29.518' },
    { id: 'AUSF', icon: '🪪', title: '身分驗證員', job: '和 UDM 合作確認這張 SIM 是否可信，但不直接搬運使用者資料。', spec: '5G-AKA · TS 33.501' },
    { id: 'UDM', icon: '🗂️', title: '用戶資料管家', job: '管理訂閱、金鑰與使用者身分；free5GC 會搭配 UDR 儲存資料。', spec: 'Nudm · TS 29.503' },
    { id: 'NRF', icon: '📒', title: '核網電話簿', job: '每個 NF 先來登記，再透過它找到要合作的其他 NF。', spec: 'SBI · TS 29.510' },
    { id: 'NSSF', icon: '🛤️', title: '切片領航員', job: '依服務需求選擇合適的網路切片，例如高速、低延遲或大量 IoT。', spec: 'S-NSSAI · TS 29.531' },
    { id: 'SMF', icon: '🎛️', title: '資料旅程規劃師', job: '建立 PDU Session、分配 IP，並透過 N4 告訴 UPF 封包該怎麼走。', spec: 'N4 / PFCP · TS 29.502' },
    { id: 'PCF', icon: '📋', title: '規則制定者', job: '決定 QoS、存取與計費政策，SMF 依它的規則安排流量。', spec: 'Npcf · TS 29.507' },
    { id: 'UPF', icon: '🚚', title: '真正搬運封包的快遞員', job: '唯一位於使用者平面的主要 NF，把 UE 資料送往 Internet，並執行 QoS。', spec: 'N3 / N6 · TS 29.244' },
];
const JOURNEY = [
    { title: '手機先報到', path: 'UE → gNB → AMF', body: 'UE 透過基地台送出 Registration Request。這一段是控制訊號，不是影片或網頁資料。' },
    { title: '核對身分', path: 'AMF → AUSF ↔ UDM', body: 'AMF 找 AUSF 驗證，AUSF 再向 UDM 取得驗證資料；成功後才允許繼續。' },
    { title: '選路與建立連線', path: 'AMF → NSSF → SMF → UPF', body: 'NSSF 協助選切片，SMF 建立 PDU Session，並用 PFCP 設定 UPF 的轉送規則。' },
    { title: '資料開始流動', path: 'UE ⇄ gNB ⇄ UPF ⇄ Internet', body: '控制面任務完成後，真正的城市流量走 N3/N6；AMF、AUSF 不會經手每一個資料封包。' },
];
export function LearningCenter({ onClose }: Props) {
    const [tab, setTab] = useState<'map' | 'nf' | 'journey'>('map');
    const [selected, setSelected] = useState('AMF');
    const [step, setStep] = useState(0);
    const { free5gcStatus, metrics, pods, activeEvent } = useAppStore();
    const nf = NFS.find((item) => item.id === selected) ?? NFS[0];
    const running = useMemo(() => pods.reduce((sum, group) => sum + group.pods.filter((pod) => pod.phase === 'Running').length, 0), [pods]);
    return <div className="fixed inset-0 z-50 overflow-y-auto bg-slate-950/70 p-3 backdrop-blur-sm sm:p-6" role="dialog" aria-modal="true" aria-label={'5GC 新手導覽'}>
    <div className="mx-auto max-w-6xl overflow-hidden rounded-2xl border border-primary-200 bg-slate-50 shadow-2xl">
      <header className="bg-gradient-to-r from-slate-950 via-primary-950 to-primary-950 px-5 py-5 text-white sm:px-8">
        <div className="flex items-start justify-between gap-4"><div><p className="text-xs font-bold uppercase tracking-[.28em] text-primary-300">5G Core · Learn by doing</p><h1 className="mt-2 text-2xl font-black sm:text-3xl">{'把 5G 核網想成一座會合作的智慧城市'}</h1><p className="mt-2 max-w-3xl text-sm text-primary-100">{'先看誰負責什麼，再跟著一支手機走完整段旅程；畫面中的數字直接連到本系統的 free5GC。'}</p></div><button onClick={onClose} className="rounded-full border border-white/30 px-3 py-1 text-xl hover:bg-white/10" aria-label={'關閉'}>×</button></div>
        <div className="mt-5 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4"><Live label={'核網狀態'} value={free5gcStatus?.connected ? '已連線' : '離線'}/><Live label={'已註冊 UE'} value={String(free5gcStatus?.registeredUeCount ?? 0)}/><Live label={'執行中 Pod'} value={String(running)}/><Live label={'目前城市事件'} value={activeEvent ? String(activeEvent) : '尚未啟動'}/></div>
      </header>

      <nav className="flex gap-1 border-b border-slate-200 bg-white p-2" aria-label={'導覽章節'}>
        {([['map', '①', '先懂全貌'], ['nf', '②', '認識 NF'], ['journey', '③', '跟著 UE 走']] as const).map(([id, no, label]) => <button key={id} onClick={() => setTab(id)} className={`flex-1 rounded-lg px-2 py-3 text-sm font-bold ${tab === id ? 'bg-primary-600 text-white shadow' : 'text-slate-600 hover:bg-slate-100'}`}>{no} {label}</button>)}
      </nav>

      <main className="p-5 sm:p-8">
        {tab === 'map' && <div><Intro title={'不用先背縮寫：先記住三件事'} subtitle={'5GC 的價值不是某一個超級設備，而是一群各司其職、能彈性擴縮的服務。'}/><div className="mt-6 grid gap-4 md:grid-cols-3"><Pillar icon="🤝" title={'服務化協作'} body={'NF 像城市局處，以 HTTP/2 API 合作；NRF 就是服務電話簿。'}/><Pillar icon="☁️" title={'雲原生'} body={'free5GC 把 NF 跑成 Kubernetes 工作負載，哪個忙就能獨立擴充。'}/><Pillar icon="↔️" title={'控制面 / 使用者面分離'} body={'AMF、SMF 負責決策；UPF 專心高速搬運封包。想像導航中心與貨運道路分工。'}/></div><div className="mt-6 rounded-xl border border-primary-200 bg-primary-50 p-4 text-sm text-slate-700"><b>{'在本系統找證據：'}</b> {`現在量測到 ${metrics?.pduSessionCount ?? 0} 個 PDU Session、${metrics?.gtpPacketsPerSec ?? 0} GTP pkt/s。前者是 SMF 建立的「連線旅程」，後者是 UPF 正在搬運的資料。`}</div></div>}

        {tab === 'nf' && <div><Intro title={'每個縮寫，先換成一個熟悉角色'} subtitle={'點選角色卡；先讀一句人話，再看介面與規格。'}/><div className="mt-6 grid gap-5 lg:grid-cols-[1.15fr_.85fr]"><div className="grid grid-cols-2 gap-2 sm:grid-cols-4">{NFS.map((item) => <button key={item.id} onClick={() => setSelected(item.id)} className={`rounded-xl border p-3 text-left transition ${selected === item.id ? 'border-primary-500 bg-primary-600 text-white shadow-lg' : 'border-slate-200 bg-white hover:-translate-y-0.5 hover:border-primary-300'}`}><span className="text-2xl">{item.icon}</span><strong className="mt-2 block text-lg">{item.id}</strong><span className={`block text-xs ${selected === item.id ? 'text-primary-100' : 'text-slate-500'}`}>{item.title}</span></button>)}</div><article className="rounded-2xl border border-primary-200 bg-white p-6 shadow-sm"><div className="text-5xl">{nf.icon}</div><p className="mt-4 text-sm font-bold text-primary-600">{nf.id}</p><h2 className="text-2xl font-black text-slate-900">{nf.title}</h2><p className="mt-4 leading-7 text-slate-600">{nf.job}</p><p className="mt-5 rounded-lg bg-slate-100 px-3 py-2 font-mono text-xs text-slate-600">{nf.spec}</p></article></div></div>}

        {tab === 'journey' && <div><Intro title={'一支手機連上網路，核網發生了什麼？'} subtitle={'一次只看一步，分清楚「做決策的控制訊號」與「真正承載內容的使用者資料」。'}/><div className="mt-6 grid gap-5 md:grid-cols-[.7fr_1.3fr]"><ol className="space-y-2">{JOURNEY.map((item, i) => <li key={item.path}><button onClick={() => setStep(i)} className={`w-full rounded-xl border p-3 text-left ${step === i ? 'border-primary-500 bg-primary-600 text-white' : 'border-slate-200 bg-white'}`}><span className="mr-2 font-black">{i + 1}</span>{item.title}</button></li>)}</ol><article className="relative overflow-hidden rounded-2xl bg-slate-950 p-6 text-white"><p className="text-xs font-bold uppercase tracking-widest text-primary-300">Step {step + 1} / {JOURNEY.length}</p><h2 className="mt-2 text-2xl font-black">{JOURNEY[step].title}</h2><div className="my-6 overflow-x-auto rounded-xl border border-primary-400/30 bg-primary-950/60 p-5 text-center font-mono text-lg font-bold text-primary-200">{JOURNEY[step].path}</div><p className="leading-7 text-slate-300">{JOURNEY[step].body}</p><div className="mt-6 flex justify-between"><button disabled={step === 0} onClick={() => setStep(v => v - 1)} className="rounded-lg border border-white/20 px-4 py-2 text-sm disabled:opacity-30">← {'上一步'}</button><button disabled={step === JOURNEY.length - 1} onClick={() => setStep(v => v + 1)} className="rounded-lg bg-primary-400 px-4 py-2 text-sm font-bold text-slate-950 disabled:opacity-30">{'下一步'} →</button></div></article></div></div>}
      </main>
    </div>
  </div>;
}
function Intro({ title, subtitle }: {
    title: string;
    subtitle: string;
}) { return <div><h2 className="text-2xl font-black text-slate-900">{title}</h2><p className="mt-2 text-slate-600">{subtitle}</p></div>; }
function Pillar({ icon, title, body }: {
    icon: string;
    title: string;
    body: string;
}) { return <article className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><span className="text-3xl">{icon}</span><h3 className="mt-3 text-lg font-black text-slate-900">{title}</h3><p className="mt-2 text-sm leading-6 text-slate-600">{body}</p></article>; }
function Live({ label, value }: {
    label: string;
    value: string;
}) { return <div className="rounded-lg border border-white/15 bg-white/10 p-2"><span className="block text-primary-200">{label}</span><strong className="mt-1 block truncate text-white" title={value}>{value}</strong></div>; }
