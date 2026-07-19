import { useLocale } from '../../i18n'

const FREE5GC_WEBUI_URL = (import.meta.env.VITE_FREE5GC_WEBUI_URL ?? '').trim()

export function Free5gcWebuiLink() {
  const { text } = useLocale()

  if (!FREE5GC_WEBUI_URL) {
    return (
      <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-500" role="status">
        {text('free5GC 後台連結尚未設定。', 'The free5GC WebUI link is not configured.')}
      </div>
    )
  }

  return (
    <a
      href={FREE5GC_WEBUI_URL}
      target="_blank"
      rel="noopener noreferrer"
      className="mt-4 flex min-h-11 items-center justify-between gap-4 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm font-bold text-blue-700 transition hover:border-blue-400 hover:bg-blue-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2"
      aria-label={text('在新分頁開啟 free5GC 後台', 'Open the free5GC WebUI in a new tab')}
    >
      <span>
        <span className="block">{text('開啟 free5GC 後台', 'Open free5GC WebUI')}</span>
        <span className="mt-0.5 block text-xs font-normal text-slate-500">
          {text('在新分頁查看訂閱者與核心網設定（需可連入 VPC）', 'View subscribers and core-network settings in a new tab (VPC access required)')}
        </span>
      </span>
      <span className="text-lg" aria-hidden="true">↗</span>
    </a>
  )
}
