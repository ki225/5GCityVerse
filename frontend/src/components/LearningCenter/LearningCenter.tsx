import { useMemo, useState } from 'react'
import { useLocale } from '../../i18n'
import { useAppStore } from '../../store/appStore'

type Props = { onClose: () => void }

const NFS = [
  { id: 'AMF', icon: '🚪', zh: '核網的大門與交通警察', en: 'The core front door', jobZh: '接住基地台送來的訊號，管理 UE 註冊、連線與移動。', jobEn: 'Receives signaling from the gNB and manages UE registration, connection, and mobility.', spec: 'N1 / N2 · TS 29.518' },
  { id: 'AUSF', icon: '🪪', zh: '身分驗證員', en: 'The identity checker', jobZh: '和 UDM 合作確認這張 SIM 是否可信，但不直接搬運使用者資料。', jobEn: 'Works with UDM to verify whether the SIM is trusted; it does not carry user traffic.', spec: '5G-AKA · TS 33.501' },
  { id: 'UDM', icon: '🗂️', zh: '用戶資料管家', en: 'The subscriber keeper', jobZh: '管理訂閱、金鑰與使用者身分；free5GC 會搭配 UDR 儲存資料。', jobEn: 'Manages subscriptions, keys, and identities; free5GC stores the data through UDR.', spec: 'Nudm · TS 29.503' },
  { id: 'NRF', icon: '📒', zh: '核網電話簿', en: 'The service directory', jobZh: '每個 NF 先來登記，再透過它找到要合作的其他 NF。', jobEn: 'Network functions register here and use it to discover one another.', spec: 'SBI · TS 29.510' },
  { id: 'NSSF', icon: '🛤️', zh: '切片領航員', en: 'The slice navigator', jobZh: '依服務需求選擇合適的網路切片，例如高速、低延遲或大量 IoT。', jobEn: 'Selects the right slice for broadband, low-latency, or massive-IoT needs.', spec: 'S-NSSAI · TS 29.531' },
  { id: 'SMF', icon: '🎛️', zh: '資料旅程規劃師', en: 'The session conductor', jobZh: '建立 PDU Session、分配 IP，並透過 N4 告訴 UPF 封包該怎麼走。', jobEn: 'Creates PDU sessions, assigns IPs, and programs UPF forwarding over N4.', spec: 'N4 / PFCP · TS 29.502' },
  { id: 'PCF', icon: '📋', zh: '規則制定者', en: 'The policy maker', jobZh: '決定 QoS、存取與計費政策，SMF 依它的規則安排流量。', jobEn: 'Defines QoS, access, and charging policy used by the SMF.', spec: 'Npcf · TS 29.507' },
  { id: 'UPF', icon: '🚚', zh: '真正搬運封包的快遞員', en: 'The packet courier', jobZh: '唯一位於使用者平面的主要 NF，把 UE 資料送往 Internet，並執行 QoS。', jobEn: 'The main user-plane NF; forwards UE data to the Internet and enforces QoS.', spec: 'N3 / N6 · TS 29.244' },
]

const JOURNEY = [
  { titleZh: '手機先報到', titleEn: 'UE checks in', path: 'UE → gNB → AMF', bodyZh: 'UE 透過基地台送出 Registration Request。這一段是控制訊號，不是影片或網頁資料。', bodyEn: 'The UE sends a Registration Request through the gNB. This is control signaling, not app data.' },
  { titleZh: '核對身分', titleEn: 'Identity is verified', path: 'AMF → AUSF ↔ UDM', bodyZh: 'AMF 找 AUSF 驗證，AUSF 再向 UDM 取得驗證資料；成功後才允許繼續。', bodyEn: 'AMF asks AUSF to authenticate the UE, while AUSF obtains authentication data from UDM.' },
  { titleZh: '選路與建立連線', titleEn: 'A route is prepared', path: 'AMF → NSSF → SMF → UPF', bodyZh: 'NSSF 協助選切片，SMF 建立 PDU Session，並用 PFCP 設定 UPF 的轉送規則。', bodyEn: 'NSSF helps select a slice; SMF creates the PDU session and programs UPF with PFCP.' },
  { titleZh: '資料開始流動', titleEn: 'Packets start moving', path: 'UE ⇄ gNB ⇄ UPF ⇄ Internet', bodyZh: '控制面任務完成後，真正的城市流量走 N3/N6；AMF、AUSF 不會經手每一個資料封包。', bodyEn: 'Once control-plane setup is done, city traffic crosses N3/N6; AMF and AUSF do not carry every packet.' },
]

export function LearningCenter({ onClose }: Props) {
  const { text } = useLocale()
  const [tab, setTab] = useState<'map' | 'nf' | 'journey'>('map')
  const [selected, setSelected] = useState('AMF')
  const [step, setStep] = useState(0)
  const { free5gcStatus, metrics, pods, activeEvent } = useAppStore()
  const nf = NFS.find((item) => item.id === selected) ?? NFS[0]
  const running = useMemo(() => pods.reduce((sum, group) => sum + group.pods.filter((pod) => pod.phase === 'Running').length, 0), [pods])

  return <div className="fixed inset-0 z-50 overflow-y-auto bg-slate-950/70 p-3 backdrop-blur-sm sm:p-6" role="dialog" aria-modal="true" aria-label={text('5GC 新手導覽', '5GC beginner guide')}>
    <div className="mx-auto max-w-6xl overflow-hidden rounded-2xl border border-blue-200 bg-[#f8fafc] shadow-2xl">
      <header className="bg-gradient-to-r from-slate-950 via-blue-950 to-indigo-950 px-5 py-5 text-white sm:px-8">
        <div className="flex items-start justify-between gap-4"><div><p className="text-xs font-bold uppercase tracking-[.28em] text-cyan-300">5G Core · Learn by doing</p><h1 className="mt-2 text-2xl font-black sm:text-3xl">{text('把 5G 核網想成一座會合作的智慧城市', 'Think of 5G Core as a city of cooperating services')}</h1><p className="mt-2 max-w-3xl text-sm text-blue-100">{text('先看誰負責什麼，再跟著一支手機走完整段旅程；畫面中的數字直接連到本系統的 free5GC。', 'Meet each role, then follow one phone end to end. Live numbers come from this system’s free5GC runtime.')}</p></div><button onClick={onClose} className="rounded-full border border-white/30 px-3 py-1 text-xl hover:bg-white/10" aria-label={text('關閉', 'Close')}>×</button></div>
        <div className="mt-5 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4"><Live label={text('核網狀態','Core status')} value={free5gcStatus?.connected ? text('已連線','Connected') : text('離線','Offline')} /><Live label={text('已註冊 UE','Registered UEs')} value={String(free5gcStatus?.registeredUeCount ?? 0)} /><Live label={text('執行中 Pod','Running pods')} value={String(running)} /><Live label={text('目前城市事件','Active event')} value={activeEvent ? String(activeEvent) : text('尚未啟動','None')} /></div>
      </header>

      <nav className="flex gap-1 border-b border-slate-200 bg-white p-2" aria-label={text('導覽章節','Guide chapters')}>
        {([['map', '①', text('先懂全貌','Big picture')], ['nf', '②', text('認識 NF','Meet the NFs')], ['journey', '③', text('跟著 UE 走','Follow a UE')]] as const).map(([id, no, label]) => <button key={id} onClick={() => setTab(id)} className={`flex-1 rounded-lg px-2 py-3 text-sm font-bold ${tab === id ? 'bg-blue-600 text-white shadow' : 'text-slate-600 hover:bg-slate-100'}`}>{no} {label}</button>)}
      </nav>

      <main className="p-5 sm:p-8">
        {tab === 'map' && <div><Intro title={text('不用先背縮寫：先記住三件事','No acronyms yet—remember three ideas')} subtitle={text('5GC 的價值不是某一個超級設備，而是一群各司其職、能彈性擴縮的服務。','5GC is not one super box; it is a team of independently scalable services.')} /><div className="mt-6 grid gap-4 md:grid-cols-3"><Pillar icon="🤝" title={text('服務化協作','Service-based')} body={text('NF 像城市局處，以 HTTP/2 API 合作；NRF 就是服務電話簿。','NFs cooperate over HTTP/2 APIs; NRF is their service directory.')} /><Pillar icon="☁️" title={text('雲原生','Cloud-native')} body={text('free5GC 把 NF 跑成 Kubernetes 工作負載，哪個忙就能獨立擴充。','free5GC runs NFs as Kubernetes workloads so busy roles can scale independently.')} /><Pillar icon="↔️" title={text('控制面 / 使用者面分離','Control / user plane split')} body={text('AMF、SMF 負責決策；UPF 專心高速搬運封包。想像導航中心與貨運道路分工。','AMF and SMF decide; UPF forwards packets—like a control center separated from freight roads.')} /></div><div className="mt-6 rounded-xl border border-cyan-200 bg-cyan-50 p-4 text-sm text-slate-700"><b>{text('在本系統找證據：','See it in this system:')}</b> {text(`現在量測到 ${metrics?.pduSessionCount ?? 0} 個 PDU Session、${metrics?.gtpPacketsPerSec ?? 0} GTP pkt/s。前者是 SMF 建立的「連線旅程」，後者是 UPF 正在搬運的資料。`, `There are ${metrics?.pduSessionCount ?? 0} PDU sessions and ${metrics?.gtpPacketsPerSec ?? 0} GTP pkt/s. Sessions are arranged by SMF; packets are carried by UPF.`)}</div></div>}

        {tab === 'nf' && <div><Intro title={text('每個縮寫，先換成一個熟悉角色','Turn every acronym into a familiar role')} subtitle={text('點選角色卡；先讀一句人話，再看介面與規格。','Pick a card: plain language first, interfaces and specs second.')} /><div className="mt-6 grid gap-5 lg:grid-cols-[1.15fr_.85fr]"><div className="grid grid-cols-2 gap-2 sm:grid-cols-4">{NFS.map((item) => <button key={item.id} onClick={() => setSelected(item.id)} className={`rounded-xl border p-3 text-left transition ${selected === item.id ? 'border-blue-500 bg-blue-600 text-white shadow-lg' : 'border-slate-200 bg-white hover:-translate-y-0.5 hover:border-blue-300'}`}><span className="text-2xl">{item.icon}</span><strong className="mt-2 block text-lg">{item.id}</strong><span className={`block text-xs ${selected === item.id ? 'text-blue-100' : 'text-slate-500'}`}>{text(item.zh,item.en)}</span></button>)}</div><article className="rounded-2xl border border-blue-200 bg-white p-6 shadow-sm"><div className="text-5xl">{nf.icon}</div><p className="mt-4 text-sm font-bold text-blue-600">{nf.id}</p><h2 className="text-2xl font-black text-slate-900">{text(nf.zh,nf.en)}</h2><p className="mt-4 leading-7 text-slate-600">{text(nf.jobZh,nf.jobEn)}</p><p className="mt-5 rounded-lg bg-slate-100 px-3 py-2 font-mono text-xs text-slate-600">{nf.spec}</p></article></div></div>}

        {tab === 'journey' && <div><Intro title={text('一支手機連上網路，核網發生了什麼？','What happens when one phone connects?')} subtitle={text('一次只看一步，分清楚「做決策的控制訊號」與「真正承載內容的使用者資料」。','One step at a time: separate control decisions from the user data carrying actual content.')} /><div className="mt-6 grid gap-5 md:grid-cols-[.7fr_1.3fr]"><ol className="space-y-2">{JOURNEY.map((item,i)=><li key={item.path}><button onClick={()=>setStep(i)} className={`w-full rounded-xl border p-3 text-left ${step===i?'border-blue-500 bg-blue-600 text-white':'border-slate-200 bg-white'}`}><span className="mr-2 font-black">{i+1}</span>{text(item.titleZh,item.titleEn)}</button></li>)}</ol><article className="relative overflow-hidden rounded-2xl bg-slate-950 p-6 text-white"><p className="text-xs font-bold uppercase tracking-widest text-cyan-300">Step {step+1} / {JOURNEY.length}</p><h2 className="mt-2 text-2xl font-black">{text(JOURNEY[step].titleZh,JOURNEY[step].titleEn)}</h2><div className="my-6 overflow-x-auto rounded-xl border border-blue-400/30 bg-blue-950/60 p-5 text-center font-mono text-lg font-bold text-cyan-200">{JOURNEY[step].path}</div><p className="leading-7 text-slate-300">{text(JOURNEY[step].bodyZh,JOURNEY[step].bodyEn)}</p><div className="mt-6 flex justify-between"><button disabled={step===0} onClick={()=>setStep(v=>v-1)} className="rounded-lg border border-white/20 px-4 py-2 text-sm disabled:opacity-30">← {text('上一步','Back')}</button><button disabled={step===JOURNEY.length-1} onClick={()=>setStep(v=>v+1)} className="rounded-lg bg-cyan-400 px-4 py-2 text-sm font-bold text-slate-950 disabled:opacity-30">{text('下一步','Next')} →</button></div></article></div></div>}
      </main>
    </div>
  </div>
}

function Intro({title,subtitle}:{title:string;subtitle:string}) { return <div><h2 className="text-2xl font-black text-slate-900">{title}</h2><p className="mt-2 text-slate-600">{subtitle}</p></div> }
function Pillar({icon,title,body}:{icon:string;title:string;body:string}) { return <article className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><span className="text-3xl">{icon}</span><h3 className="mt-3 text-lg font-black text-slate-900">{title}</h3><p className="mt-2 text-sm leading-6 text-slate-600">{body}</p></article> }
function Live({label,value}:{label:string;value:string}) { return <div className="rounded-lg border border-white/15 bg-white/10 p-2"><span className="block text-blue-200">{label}</span><strong className="mt-1 block truncate text-white" title={value}>{value}</strong></div> }
