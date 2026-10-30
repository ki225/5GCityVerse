import type { Point } from '../domain/model'

/** Schematic world-grid routing; these bends do not imply physical transport geometry. */
export function ribbonWaypoints(from: Point, to: Point, local = false): Point[] {
  if (local) return [from, { x: from.x + 1.1, y: from.y }, { x: from.x + 1.1, y: from.y + 1.1 }, { x: from.x, y: from.y + 1.1 }, from]
  const corner = Math.abs(to.x - from.x) >= Math.abs(to.y - from.y)
    ? { x: to.x, y: from.y } : { x: from.x, y: to.y }
  return [from, corner, to]
}

/** Round projected corners with bounded radii, retaining exact segment endpoints. */
export function roundedRibbon(points: Point[], radius = 9): string {
  if (points.some(p => !Number.isFinite(p.x) || !Number.isFinite(p.y))) return ''
  const unique = points.filter((p, i) => i === 0 || Math.hypot(p.x - points[i - 1].x, p.y - points[i - 1].y) > .01)
  if (unique.length < 2) return ''
  let path = `M${unique[0].x},${unique[0].y}`
  for (let i = 1; i < unique.length - 1; i++) {
    const a = unique[i - 1], b = unique[i], c = unique[i + 1]
    const incoming = Math.hypot(b.x - a.x, b.y - a.y), outgoing = Math.hypot(c.x - b.x, c.y - b.y)
    const r = Math.min(Math.max(0, radius), incoming * .4, outgoing * .4)
    const before = { x: b.x + (a.x - b.x) * r / incoming, y: b.y + (a.y - b.y) * r / incoming }
    const after = { x: b.x + (c.x - b.x) * r / outgoing, y: b.y + (c.y - b.y) * r / outgoing }
    path += `L${before.x},${before.y}Q${b.x},${b.y} ${after.x},${after.y}`
  }
  const last = unique[unique.length - 1]
  return `${path}L${last.x},${last.y}`
}
