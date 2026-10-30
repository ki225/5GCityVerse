import type { Point } from '../domain/model'

/** A reversible fixed isometric camera. Model coordinates remain kilometres. */
export class IsometricProjection {
  scale = 1
  origin: Point = { x: 0, y: 0 }
  project(point: Point, height = 0): Point {
    return { x: this.origin.x + (point.x - point.y) * this.scale, y: this.origin.y + (point.x + point.y) * this.scale * 0.5 - height * this.scale }
  }
  unproject(point: Point): Point {
    const x = (point.x - this.origin.x) / this.scale
    const y = (point.y - this.origin.y) / (this.scale * 0.5)
    return { x: (x + y) / 2, y: (y - x) / 2 }
  }
  fit(width: number, height: number, viewWidth: number, viewHeight: number, zoom: number, pan: Point) {
    this.scale = Math.min((viewWidth - 64) / (width + height), (viewHeight - 96) / ((width + height) * 0.5)) * zoom
    this.scale = Math.max(1, this.scale)
    this.origin = {
      x: viewWidth * 0.5 - (width - height) * this.scale * 0.5 + pan.x,
      y: (viewHeight - (width + height) * 0.5 * this.scale) * 0.5 + pan.y + 10,
    }
  }
}
