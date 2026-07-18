import { useEffect, useState } from 'react'

export type Locale = 'zh-TW' | 'en'
const KEY = '5gcityverse.locale'
const SELECTED_KEY = '5gcityverse.locale.selected'
const EVENT = '5gcityverse-locale-change'

export function currentLocale(): Locale {
  if (typeof window === 'undefined') return 'zh-TW'
  return window.sessionStorage.getItem(KEY) === 'en' ? 'en' : 'zh-TW'
}

export function hasSelectedLocale(): boolean {
  return typeof window !== 'undefined' && window.sessionStorage.getItem(SELECTED_KEY) === 'true'
}

export function useLocale() {
  const [locale, setState] = useState<Locale>(currentLocale)
  useEffect(() => {
    const sync = () => setState(currentLocale())
    window.addEventListener(EVENT, sync)
    return () => window.removeEventListener(EVENT, sync)
  }, [])
  const setLocale = (next: Locale) => {
    window.sessionStorage.setItem(KEY, next)
    window.sessionStorage.setItem(SELECTED_KEY, 'true')
    document.documentElement.lang = next
    window.dispatchEvent(new Event(EVENT))
  }
  return { locale, setLocale, text: (zh: string, en: string) => locale === 'zh-TW' ? zh : en }
}
