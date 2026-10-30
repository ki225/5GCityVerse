import { CityObject } from '../domain/model'
import type { ObjectPainter } from './painters/painters'
import type { CityDemand } from '../domain/demand'

export class DemandMarker extends CityObject {
  constructor(public demand: CityDemand) {
    super('demand-marker', 'network', { id: `demand-marker:${demand.groupId}`, label: demand.label,
      ...demand.sourceLocation.position, width: .3, depth: .3, height: .45 })
  }
}
export const paintDemandMarker: ObjectPainter = (object, { ctx, projection }) => {
  const row = (object as DemandMarker).demand, point = projection.project(object.position, .6)
  ctx.save(); ctx.font = 'bold 11px system-ui'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
  const text = `${row.temporary ? '◆ ' : ''}${row.ueCount} UE`, width = ctx.measureText(text).width + 16
  ctx.fillStyle = row.temporary ? '#b2693c' : '#426a50'; ctx.beginPath(); ctx.roundRect(point.x - width / 2, point.y - 13, width, 23, 7); ctx.fill()
  ctx.beginPath(); ctx.moveTo(point.x - 4, point.y + 9); ctx.lineTo(point.x, point.y + 16); ctx.lineTo(point.x + 4, point.y + 9); ctx.fill()
  ctx.fillStyle = '#fff'; ctx.fillText(text, point.x, point.y - 1); ctx.restore()
}
