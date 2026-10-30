import { DataCenter } from '../../domain/model'
import type { Bounds, CityObject } from '../../domain/model'
import { IsometricProjection } from '../projection'
import { facade, ivory, line, prism, teal } from '../geometry'
import type { ObjectPainter, PaintContext } from '../geometry'

/** Cloud + server halls, distinct from the city's office towers without lettering. */
export const dataCenterPainter: ObjectPainter = (object, context) => {
  const site = object as DataCenter, { x, y, width: w, height: d } = object.bounds, h = object.size.height
  prism(context, { x: x - w * .1, y: y - d * .1, width: w * 1.2, height: d * 1.2 }, .1, ['#d6ddcc', '#adbba9', '#c5cfbd'])
  if (site.tier === 'central') {
    const hall = { x, y, width: w, height: d * .66 }
    prism(context, hall, h * .68, ivory, .1)
    serverFace(context, hall, h * .64, 5)
    // Cooling plant and flat-roof fans are equipment, rather than office windows.
    for (let i = 0; i < 3; i++) {
      const fan = { x: x + w * (.09 + i * .29), y: y + d * .13, width: w * .21, height: d * .25 }
      prism(context, fan, h * .1, ['#b3c8c3', '#7e9a95', '#9bb4ad'], h * .68 + .1)
      const p = context.projection.project({ x: fan.x + fan.width / 2, y: fan.y + fan.height / 2 }, h * .79 + .1)
      context.ctx.strokeStyle = '#6d9292'; context.ctx.lineWidth = .8
      context.ctx.beginPath(); context.ctx.ellipse(p.x, p.y, Math.max(1.6, fan.width * context.projection.scale * .28), Math.max(1, fan.height * context.projection.scale * .25), 0, 0, Math.PI * 2); context.ctx.stroke()
    }
    for (let i = 0; i < 3; i++) {
      const rack = { x: x + w * (.12 + i * .28), y: y + d * .74, width: w * .2, height: d * .2 }
      prism(context, rack, h * .43, ['#7b9b9b', '#2e5663', '#416f7c'], .1)
      serverFace(context, rack, h * .39, 1)
    }
    const p = context.projection.project({ x: x + w * .5, y: y + d * .25 }, h * .76 + .65)
    paintCloud(context, p.x, p.y, Math.max(24, w * context.projection.scale * .8))
  } else {
    prism(context, object.bounds, h, site.tier === 'distributed' ? ['#5d8e91', '#2e5f6c', '#487a82'] : teal, .1)
    serverFace(context, object.bounds, h)
    for (let i = 0; i < (site.tier === 'distributed' ? 3 : 2); i++) prism(context, { x: x + w * (.12 + i * .26), y: y + d * .2, width: w * .2, height: d * .45 }, h * .12, ['#c1d1c9', '#7f9c97', '#a4bdb6'], h + .1)
    if (site.tier === 'distributed') {
      const wing = { x: x - w * .25, y: y + d * .25, width: w * .3, height: d * .7 }
      prism(context, wing, h * .65, teal, .1); serverFace(context, wing, h * .65)
    }
  }
}

function serverFace(context: PaintContext, b: Bounds, height: number, bays = 3) {
  const { projection: p, ctx } = context
  for (let i = 0; i < bays; i++) {
    const left = b.x + b.width * (.07 + i * .88 / bays), right = left + b.width * .65 / bays
    // Dark rack doors with stacked slots create a server silhouette at both scales.
    facade(context, { x: left, y: b.y, width: right - left, height: b.height }, height, 4, 1, '#94bcbf', .1)
    for (let row = 0; row < 4; row++) line(context, { x: left, y: b.y + b.height }, { x: right, y: b.y + b.height }, '#acd0ca', .75, .1 + height * (.13 + row * .2))
    const led = p.project({ x: right, y: b.y + b.height }, .1 + height * .82)
    ctx.fillStyle = '#b7d9ab'; ctx.beginPath(); ctx.arc(led.x, led.y, .8, 0, Math.PI * 2); ctx.fill()
  }
}

function paintCloud({ ctx }: PaintContext, x: number, y: number, width: number) {
  const w = width, h = width * .45
  ctx.save(); ctx.translate(x - w / 2, y - h / 2)
  ctx.beginPath(); ctx.moveTo(w * .18, h * .88)
  ctx.bezierCurveTo(-w * .02, h * .88, -w * .03, h * .36, w * .2, h * .34)
  ctx.bezierCurveTo(w * .22, -h * .05, w * .57, -h * .12, w * .68, h * .25)
  ctx.bezierCurveTo(w * .94, h * .14, w * 1.06, h * .82, w * .81, h * .88)
  ctx.closePath(); ctx.fillStyle = '#eef6ed'; ctx.fill(); ctx.strokeStyle = '#6e9fa6'; ctx.lineWidth = 1.2; ctx.stroke()
  ctx.strokeStyle = '#6e9fa6'; ctx.lineWidth = 1
  ctx.beginPath(); ctx.moveTo(w * .5, h * .9); ctx.lineTo(w * .5, h * 1.24); ctx.moveTo(w * .29, h * 1.24); ctx.lineTo(w * .71, h * 1.24); ctx.stroke()
  for (const u of [.29, .5, .71]) { ctx.fillStyle = '#72a0a5'; ctx.fillRect(w * u - 1, h * 1.24, 2, 2) }
  ctx.restore()
}

/** Tri-sector radio mast, dish and RF arcs prevent confusion with power pylons. */
export const gnbPainter: ObjectPainter = (object, context) => {
  const { projection: p, ctx } = context, { x, y } = object.position, h = object.size.height
  const width = Math.max(3, p.scale * .13), base = p.project({ x, y }), top = p.project({ x, y }, h)
  prism(context, { x: x - .16, y: y - .13, width: .32, height: .26 }, .07, ['#c1cdbd', '#7c9185', '#a1b3a2'])
  prism(context, { x: x - .035, y: y - .035, width: .07, height: .07 }, h * .94, ['#d1dfd4', '#567f84', '#8facaa'])
  for (const side of [-1, 1]) {
    ctx.strokeStyle = '#6c8887'; ctx.lineWidth = .85; ctx.beginPath(); ctx.moveTo(base.x + side * width, base.y); ctx.lineTo(top.x, top.y + h * p.scale * .55); ctx.stroke()
  }
  const panelWidth = Math.max(2.7, p.scale * .13), panelHeight = Math.max(7, h * p.scale * .3)
  for (const offset of [-1, 0, 1]) {
    const px = top.x + offset * width * 1.04 - panelWidth / 2, py = top.y + Math.abs(offset) * 2
    ctx.fillStyle = '#edf1df'; ctx.strokeStyle = '#88aaa6'; ctx.lineWidth = .65
    ctx.beginPath(); ctx.roundRect(px, py, panelWidth, panelHeight, 1); ctx.fill(); ctx.stroke()
    ctx.fillStyle = '#7b9fa5'; ctx.fillRect(px + panelWidth * .72, py + 1, panelWidth * .28, panelHeight - 2)
  }
  const dishY = top.y + h * p.scale * .55
  ctx.strokeStyle = '#6d9293'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(top.x, dishY + 2); ctx.lineTo(top.x + width * 1.8, dishY); ctx.stroke()
  ctx.fillStyle = '#dbe9df'; ctx.beginPath(); ctx.ellipse(top.x + width * 1.7, dishY - 1, width * .7, width, -.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke()
  ctx.strokeStyle = '#719fa5'; ctx.lineWidth = 1; ctx.beginPath()
  for (const radius of [width * 1.7, width * 2.55]) for (const side of [0, Math.PI]) {
    ctx.moveTo(top.x + radius * Math.cos(side - .6), top.y + 1 + radius * Math.sin(side - .6))
    ctx.arc(top.x, top.y + 1, radius, side - .6, side + .6)
  }
  ctx.stroke()
}

export function paintExternalSite(object: CityObject, context: PaintContext, x: number, y: number) {
  const projection = new IsometricProjection(); projection.scale = 22
  projection.origin = { x: x - (object.position.x - object.position.y) * projection.scale, y: y - (object.position.x + object.position.y) * projection.scale * .5 }
  dataCenterPainter(object, { ...context, projection })
}
