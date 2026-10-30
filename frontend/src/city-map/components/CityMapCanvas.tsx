import { useEffect, useRef } from 'react'
import { CityMap } from '../domain/model'
import { CityMapRenderer } from '../rendering/renderer'
import type { RendererOptions } from '../rendering/renderer'

export interface CityMapCanvasProps extends RendererOptions {
  map: CityMap
  onReady?: (renderer: CityMapRenderer | null) => void
  className?: string
}

/** Backend-independent React adapter. The caller owns the scene and plugin lifecycle. */
export function CityMapCanvas(props: CityMapCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const rendererRef = useRef<CityMapRenderer | null>(null)
  const callbacks = useRef(props)
  callbacks.current = props
  useEffect(() => {
    const renderer = new CityMapRenderer(canvasRef.current!, props.map, {
      showLabels: callbacks.current.showLabels,
      onSelect: object => callbacks.current.onSelect?.(object),
      onMapClick: (point, object) => callbacks.current.onMapClick?.(point, object) ?? false,
      onStats: stats => callbacks.current.onStats?.(stats),
    })
    rendererRef.current = renderer
    callbacks.current.onReady?.(renderer)
    return () => { renderer.destroy(); rendererRef.current = null; callbacks.current.onReady?.(null) }
  }, [props.map])
  useEffect(() => { if (props.showLabels !== undefined) rendererRef.current?.setLabelsVisible(props.showLabels) }, [props.showLabels, props.map])
  return <canvas ref={canvasRef} className={props.className} aria-label="等角城市地圖，可拖曳平移與滾輪縮放；可直接點選建築設定需求或點選設備查看狀態" />
}
