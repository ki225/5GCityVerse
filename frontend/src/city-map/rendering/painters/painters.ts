import { NetworkFunction } from '../../domain/model'
import { buildingPainter } from './building-painters'
import { createActorPainters } from './actor-painters'
import { landmarkPainter } from './landmark-painters'
import { line, polygon, prism, tile } from '../geometry'
import type { ObjectPainter } from '../geometry'
import { dataCenterPainter, gnbPainter } from './infrastructure-painters'

export * from '../geometry'
export { ueColors } from './actor-painters'
export { paintExternalSite } from './infrastructure-painters'

export function builtinPainters(): Map<string, ObjectPainter> {
  const result = createActorPainters()
  for (const type of ['house', 'school', 'hospital', 'shop', 'arena', 'factory', 'warehouse', 'station']) result.set(`building:${type}`, buildingPainter)
  for (const type of ['railway', 'train', 'crane', 'ship']) result.set(`landmark:${type}`, landmarkPainter)
  result.set('road', (object, context) => {
    const b = object.bounds
    tile(context, b.x, b.y, b.width, b.height, '#c8c8b9', .012)
    const p = context.projection, ctx = context.ctx
    const a = b.width > b.height ? { x: b.x, y: object.position.y } : { x: object.position.x, y: b.y }
    const z = b.width > b.height ? { x: b.x + b.width, y: object.position.y } : { x: object.position.x, y: b.y + b.height }
    const s = p.project(a), e = p.project(z)
    ctx.beginPath(); ctx.moveTo(s.x, s.y); ctx.lineTo(e.x, e.y); ctx.strokeStyle = '#f9f8e9'; ctx.lineWidth = 1; ctx.setLineDash([5, 7]); ctx.stroke(); ctx.setLineDash([])
  })
  result.set('water', (object, context) => { const b = object.bounds; tile(context, b.x, b.y, b.width, b.height, '#86bbc2') })
  result.set('park', (object, context) => { const b = object.bounds; tile(context, b.x, b.y, b.width, b.height, '#b8cc96') })
  result.set('tree', (object, context) => {
    const { projection: p, ctx } = context, { x, y } = object.position, w = object.size.width * .65, h = object.size.height
    line(context, { x, y }, { x, y }, '#8d8765', 1.5, 0, h * .45)
    for (const offset of [0, .25]) {
      const a = p.project({ x: x - w, y }, h * (.3 + offset)), b = p.project({ x: x + w, y }, h * (.3 + offset)), top = p.project({ x, y }, h * (1 + offset))
      const mid = { x: top.x, y: a.y + 1 }
      polygon(ctx, [a, top, mid], '#94b76c'); polygon(ctx, [mid, top, b], '#6e9851')
    }
  })
  result.set('gnb', gnbPainter)
  result.set('datacenter', dataCenterPainter)
  result.set('nf', (object, context) => {
    const nf = object as NetworkFunction, b = object.bounds
    prism(context, b, Math.max(.22, object.size.height), nf.functionName === 'UPF' ? ['#79a6ab', '#366d7b', '#598e98'] : ['#aebd9b', '#6d8066', '#8d9f7f'])
    for (const h of [.08, .16]) line(context, { x: b.x + b.width * .15, y: b.y + b.height }, { x: b.x + b.width * .85, y: b.y + b.height }, '#d2dfcd', .7, h)
  })
  return result
}
