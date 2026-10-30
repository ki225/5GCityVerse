import type { Bounds, CityObject, Point } from '../domain/model'
import { IsometricProjection } from './projection'
import { colors } from '../../theme/colors'

export interface PaintContext { ctx: CanvasRenderingContext2D; projection: IsometricProjection; showLabels?: boolean }
export type ObjectPainter = (object: CityObject, context: PaintContext) => void
export type Material = readonly [top: string, left: string, right: string]
export const ivory: Material = [colors.map.ivory.top, colors.map.ivory.left, colors.map.ivory.right]
export const teal: Material = [colors.map.teal.top, colors.map.teal.left, colors.map.teal.right]

export function polygon(ctx: CanvasRenderingContext2D, points: readonly Point[], fill: string, stroke?: string) {
  ctx.beginPath(); ctx.moveTo(points[0].x, points[0].y)
  for (let i = 1; i < points.length; i++) ctx.lineTo(points[i].x, points[i].y)
  ctx.closePath(); ctx.fillStyle = fill; ctx.fill()
  if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = .6; ctx.stroke() }
}
export function tile(context: PaintContext, x: number, y: number, width: number, depth: number, fill: string, elevation = 0, stroke?: string) {
  const p = context.projection
  polygon(context.ctx, [p.project({ x, y }, elevation), p.project({ x: x + width, y }, elevation), p.project({ x: x + width, y: y + depth }, elevation), p.project({ x, y: y + depth }, elevation)], fill, stroke)
}
export function prism(context: PaintContext, bounds: Bounds, height: number, material: Material = ivory, base = 0) {
  const { x, y, width: w, height: d } = bounds, p = context.projection, ctx = context.ctx
  const a = p.project({ x, y: y + d }, base), b = p.project({ x: x + w, y: y + d }, base), c = p.project({ x: x + w, y }, base)
  const ah = p.project({ x, y: y + d }, height + base), bh = p.project({ x: x + w, y: y + d }, height + base), ch = p.project({ x: x + w, y }, height + base)
  polygon(ctx, [a, b, bh, ah], material[1]); polygon(ctx, [b, c, ch, bh], material[2])
  tile(context, x, y, w, d, material[0], height + base, '#ffffff45')
}
export function box(context: PaintContext, object: CityObject, top: string, left: string, right: string, height = object.size.height) {
  prism(context, object.bounds, height, [top, left, right])
}
export function line(context: PaintContext, a: Point, b: Point, color: string, width = 1, ah = 0, bh = ah) {
  const start = context.projection.project(a, ah), end = context.projection.project(b, bh), ctx = context.ctx
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath(); ctx.moveTo(start.x, start.y); ctx.lineTo(end.x, end.y); ctx.stroke()
}
export function facade(context: PaintContext, b: Bounds, height: number, floors = 3, columns = 3, color = '#76a4ad', base = 0) {
  const p = context.projection, { x, y, width: w, height: d } = b
  if (p.scale * Math.min(w, d) < 7) return
  for (let floor = 0; floor < floors; floor++) for (let column = 0; column < columns; column++) {
    const z = base + height * (.16 + floor * .75 / floors), z1 = z + height * .45 / floors
    const x0 = x + w * (.12 + column * .8 / columns), x1 = x0 + w * .48 / columns
    polygon(context.ctx, [p.project({ x: x0, y: y + d }, z), p.project({ x: x1, y: y + d }, z), p.project({ x: x1, y: y + d }, z1), p.project({ x: x0, y: y + d }, z1)], color)
    const y0 = y + d * (.12 + column * .8 / columns), y1 = y0 + d * .48 / columns
    polygon(context.ctx, [p.project({ x: x + w, y: y0 }, z), p.project({ x: x + w, y: y1 }, z), p.project({ x: x + w, y: y1 }, z1), p.project({ x: x + w, y: y0 }, z1)], '#8bb4ba')
  }
}
export function shadow(context: PaintContext, b: Bounds) {
  tile(context, b.x + .12, b.y + .12, b.width, b.height, '#64765a22', .008)
}
export function gable(context: PaintContext, b: Bounds, height: number, rise: number) {
  const p = context.projection, { x, y, width: w, height: d } = b
  const a = p.project({ x, y }, height), b1 = p.project({ x: x + w, y }, height)
  const c = p.project({ x: x + w, y: y + d }, height), d1 = p.project({ x, y: y + d }, height)
  const ridgeA = p.project({ x, y: y + d / 2 }, height + rise), ridgeB = p.project({ x: x + w, y: y + d / 2 }, height + rise)
  polygon(context.ctx, [b1, c, ridgeB], '#e5d9bc')
  polygon(context.ctx, [a, b1, ridgeB, ridgeA], '#e0aa78')
  polygon(context.ctx, [ridgeA, ridgeB, c, d1], '#c58156')
}
export function textAt(context: PaintContext, text: string, x: number, y: number, color = '#24443b', size = 11) {
  if (context.showLabels === false) return
  const ctx = context.ctx
  ctx.font = `600 ${size}px system-ui, sans-serif`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
  ctx.lineWidth = 3; ctx.strokeStyle = '#ffffffed'; ctx.strokeText(text, x, y)
  ctx.fillStyle = color; ctx.fillText(text, x, y)
}
