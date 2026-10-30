import { IsometricProjection } from './projection'

/** Supersample small screens too, while bounding the memory used by each surface. */
export function canvasPixelRatio(width: number, height: number, deviceRatio: number) {
  return Math.min(3, Math.max(2, deviceRatio || 1), Math.sqrt(4_000_000 / Math.max(1, width * height)))
}

/** A high-detail viewport captured from vector geometry at the actual camera scale. */
export class ViewportDetailCache {
  private surface = document.createElement('canvas')
  private ctx = this.surface.getContext('2d', { alpha: false })!
  private camera = new IsometricProjection()
  private stamp = ''
  builds = 0

  resize(width: number, height: number) {
    if (this.surface.width !== width) this.surface.width = width
    if (this.surface.height !== height) this.surface.height = height
    this.stamp = ''
  }
  capture(camera: IsometricProjection, ratio: number, stamp: string, paint: (ctx: CanvasRenderingContext2D) => void) {
    this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
    paint(this.ctx)
    this.camera.scale = camera.scale; this.camera.origin = { ...camera.origin }
    this.stamp = stamp; this.builds++
  }
  composite(ctx: CanvasRenderingContext2D, camera: IsometricProjection, width: number, height: number, stamp: string) {
    if (!this.stamp || this.stamp !== stamp) return
    const scale = camera.scale / this.camera.scale
    ctx.save()
    ctx.translate(camera.origin.x - this.camera.origin.x * scale, camera.origin.y - this.camera.origin.y * scale)
    ctx.scale(scale, scale)
    ctx.drawImage(this.surface, 0, 0, width, height)
    ctx.restore()
  }
  destroy() { this.surface.width = 0; this.surface.height = 0; this.stamp = '' }
}
