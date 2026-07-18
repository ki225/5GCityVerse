import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

const BOTTOM_THRESHOLD_PX = 30

interface UseStickToBottomResult {
  containerRef: RefObject<HTMLDivElement>
  newCount: number
  scrollToBottom: () => void
}

// Standard chat/log auto-scroll behavior: while the user is at (or near) the bottom,
// new content keeps them pinned to the bottom. Once they scroll up to read earlier
// entries, new content no longer yanks the scroll position — instead newCount tracks
// how many items arrived since they scrolled away, for a "N new" affordance that calls
// scrollToBottom() on click.
export function useStickToBottom<T>(items: T[]): UseStickToBottomResult {
  const containerRef = useRef<HTMLDivElement>(null)
  const [newCount, setNewCount] = useState(0)
  const isAtBottomRef = useRef(true)
  const prevLengthRef = useRef(items.length)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const handleScroll = () => {
      const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
      const atBottom = distanceFromBottom < BOTTOM_THRESHOLD_PX
      isAtBottomRef.current = atBottom
      if (atBottom) setNewCount(0)
    }
    el.addEventListener('scroll', handleScroll)
    return () => el.removeEventListener('scroll', handleScroll)
  }, [])

  useEffect(() => {
    const el = containerRef.current
    const added = items.length - prevLengthRef.current
    prevLengthRef.current = items.length
    if (!el || added <= 0) return
    if (isAtBottomRef.current) {
      el.scrollTop = el.scrollHeight
      setNewCount(0)
    } else {
      setNewCount((count) => count + added)
    }
  }, [items])

  function scrollToBottom() {
    const el = containerRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    isAtBottomRef.current = true
    setNewCount(0)
  }

  return { containerRef, newCount, scrollToBottom }
}
