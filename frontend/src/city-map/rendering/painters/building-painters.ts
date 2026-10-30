import { Building } from '../../domain/model'
import { facade, gable, ivory, line, polygon, prism, shadow, tile } from '../geometry'
import type { Material, ObjectPainter, PaintContext } from '../geometry'
import type { Bounds } from '../../domain/model'

/** A building's parts are local geometry; its footprint, identity and position remain one object. */
export const buildingPainter: ObjectPainter = (object, context) => {
  const building = object as Building, b = building.bounds, { x, y, width: w, height: d } = b
  const h = Math.max(.1, building.size.height)
  const local = (u: number, v: number, width: number, depth: number): Bounds => ({ x: x + u * w, y: y + v * d, width: width * w, height: depth * d })
  const part = (u: number, v: number, width: number, depth: number, height: number, material: Material = ivory, base = 0) => {
    const bounds = local(u, v, width, depth); prism(context, bounds, height, material, base); return bounds
  }
  shadow(context, b)
  if (building.buildingType === 'house') {
    const home = part(.08, .08, .8, .78, h)
    facade(context, home, h, 1, 2)
    gable(context, { ...home, x: home.x - w * .04, y: home.y - d * .04, width: home.width + w * .08, height: home.height + d * .08 }, h, h * .48)
    part(.16, .25, .13, .13, h * .62, ['#e5d7bd', '#af9e86', '#c9bba0'], h * .92)
    const door = local(.42, .85, .15, .01)
    facade(context, door, h * .65, 1, 1, '#6d897f')
    tile(context, x + w * .4, y + d * .88, w * .2, d * .12, '#c8c8ac')
  } else if (building.buildingType === 'hospital') {
    const main = part(.18, .02, .64, .55, h * 1.25)
    facade(context, main, h * 1.25, 3, 4)
    const wingA = part(0, .28, .22, .65, h * .75), wingB = part(.78, .28, .22, .65, h * .75)
    facade(context, wingA, h * .75, 2, 2); facade(context, wingB, h * .75, 2, 2)
    part(.25, .57, .5, .36, h * .43)
    tile(context, x + w * .34, y + d * .8, w * .32, d * .22, '#7ea4a5', h * .46)
    // Medical cross is geometry, so it remains visible with all text disabled.
    tile(context, x + w * .46, y + d * .15, w * .1, d * .28, '#c96f59', h * 1.26)
    tile(context, x + w * .36, y + d * .24, w * .3, d * .1, '#c96f59', h * 1.27)
  } else if (building.buildingType === 'school') {
    tile(context, x, y, w, d, '#c3cf9c')
    const main = part(0, 0, .88, .35, h * .7, ['#f1e5cd', '#c6a77b', '#ddc7a2'])
    facade(context, main, h * .7, 2, 5)
    const wing = part(.02, .3, .22, .6, h * .62, ['#f1e5cd', '#c6a77b', '#ddc7a2'])
    facade(context, wing, h * .62, 2, 2)
    const tower = part(.65, .04, .19, .22, h * 1.6)
    gable(context, tower, h * 1.6, h * .24)
    const clock = context.projection.project({ x: tower.x + tower.width / 2, y: tower.y + tower.height }, h * 1.3)
    const ctx = context.ctx, radius = Math.max(2, Math.min(4, w * context.projection.scale * .05))
    ctx.fillStyle = '#f5f0d8'; ctx.beginPath(); ctx.arc(clock.x, clock.y, radius, 0, Math.PI * 2); ctx.fill()
    ctx.strokeStyle = '#6d7b70'; ctx.lineWidth = .8; ctx.beginPath(); ctx.moveTo(clock.x, clock.y - radius * .65); ctx.lineTo(clock.x, clock.y); ctx.lineTo(clock.x + radius * .5, clock.y); ctx.stroke()
    tile(context, x + w * .34, y + d * .49, w * .57, d * .39, '#b0c18e', .015, '#e4e8cf')
    line(context, { x: x + w * .62, y: y + d * .5 }, { x: x + w * .62, y: y + d * .87 }, '#e7e9d6', .8)
  } else if (building.buildingType === 'factory') {
    part(0, .1, .96, .8, h * .65, ['#c9d0c4', '#a8b7ab', '#c2ccc1'])
    for (let i = 0; i < 3; i++) {
      const roof = local(.02 + i * .31, .12, .3, .74)
      gable(context, roof, h * .65, h * .4)
    }
    for (let i = 0; i < 2; i++) {
      const chimney = local(.12 + i * .23, .15, .1, .13)
      prism(context, chimney, h * 1.8, ['#e5dfc7', '#9da99a', '#c4c7ae'])
      prism(context, chimney, h * .15, ['#d6a076', '#b27b59', '#c88b65'], h * 1.25)
      prism(context, chimney, h * .13, ['#d6a076', '#b27b59', '#c88b65'], h * 1.6)
    }
    facade(context, local(.55, .22, .35, .68), h * .5, 1, 2, '#729398')
  } else if (building.buildingType === 'warehouse') {
    const shed = part(.02, .08, .94, .8, h * .7)
    gable(context, shed, h * .7, h * .4)
    for (let i = 0; i < 3; i++) {
      const bay = local(.1 + i * .28, .875, .21, .015)
      facade(context, bay, h * .65, 1, 1, '#69858a')
    }
    part(.05, .92, .9, .08, h * .15, ['#c4bba1', '#9d9f87', '#b0ac92'])
  } else if (building.buildingType === 'station') {
    tile(context, x, y, w, d, '#bdc5b7')
    part(.02, .05, .96, .9, h * .18)
    canopy(context, local(.05, .12, .9, .72), h * .35, h * .65)
    for (let i = 0; i < 5; i++) {
      const px = x + w * (.1 + i * .2)
      line(context, { x: px, y: y + d * .16 }, { x: px, y: y + d * .16 }, '#e8e9d9', 1.3, h * .18, h * .6)
      line(context, { x: px, y: y + d * .84 }, { x: px, y: y + d * .84 }, '#e8e9d9', 1.3, h * .18, h * .6)
    }
  } else if (building.buildingType === 'arena') {
    arena(context, b, h)
  } else {
    const tower = part(.05, .08, .78, .8, h)
    facade(context, tower, h, Math.max(2, Math.round(h / .35)), 3)
    part(.08, .1, .71, .72, h * .055, ['#dce2d3', '#9dab9c', '#c5d0bf'], h)
    part(.18, .2, .26, .25, h * .16, ['#c4d0c5', '#899d93', '#a7b8ac'], h * 1.06)
    part(.63, .63, .35, .32, h * .32)
  }
}

function canopy(context: PaintContext, b: Bounds, base: number, rise: number) {
  const p = context.projection, segments = 8
  for (let i = 0; i < segments; i++) {
    const y0 = b.y + b.height * i / segments, y1 = b.y + b.height * (i + 1) / segments
    const h0 = base + rise * Math.sin(Math.PI * i / segments), h1 = base + rise * Math.sin(Math.PI * (i + 1) / segments)
    polygon(context.ctx, [p.project({ x: b.x, y: y0 }, h0), p.project({ x: b.x + b.width, y: y0 }, h0), p.project({ x: b.x + b.width, y: y1 }, h1), p.project({ x: b.x, y: y1 }, h1)], i < 4 ? '#a3c4c2' : '#6e9da7', '#dbe8de')
    for (const f of [0, .25, .5, .75, 1]) line(context, { x: b.x + b.width * f, y: y0 }, { x: b.x + b.width * f, y: y1 }, '#f0efe0', 1, h0, h1)
  }
}

function arena(context: PaintContext, b: Bounds, height: number) {
  const p = context.projection, ctx = context.ctx, cx = b.x + b.width / 2, cy = b.y + b.height / 2
  const point = (angle: number, ratio: number, elevation: number) => p.project({ x: cx + Math.cos(angle) * b.width * .5 * ratio, y: cy + Math.sin(angle) * b.height * .5 * ratio }, elevation)
  tile(context, b.x, b.y, b.width, b.height, '#bdc9ad')
  // Arena bowl, white perimeter, stepped seats and a dark stage remain distinct without words.
  const floor = Array.from({ length: 40 }, (_, i) => point(i * Math.PI / 20, .82, .03))
  polygon(ctx, floor, '#d9c29c')
  for (let i = 0; i < 40; i++) {
    const a = i * Math.PI / 20, z = (i + 1) * Math.PI / 20
    polygon(ctx, [point(a, 1, 0), point(z, 1, 0), point(z, 1, height), point(a, 1, height)], i < 20 ? '#b7c3b8' : '#d7ded0')
    polygon(ctx, [point(a, 1, height), point(z, 1, height), point(z, .78, height * .75), point(a, .78, height * .75)], '#f2f0de', '#e5e9d7')
    polygon(ctx, [point(a, .78, height * .75), point(z, .78, height * .75), point(z, .68, height * .3), point(a, .68, height * .3)], i % 4 ? '#d8a578' : '#edf0df')
  }
  const stage = { x: cx + b.width * .19, y: cy - b.height * .24, width: b.width * .14, height: b.height * .47 }
  prism(context, stage, height * .35, ['#405d61', '#294c52', '#365b61'])
  prism(context, { ...stage, width: b.width * .06 }, height * 1.2, ['#516568', '#24474d', '#36575d'])
  for (const v of [-.17, 0, .17]) {
    const light = p.project({ x: stage.x, y: cy + b.height * v }, height * .92)
    ctx.fillStyle = '#d8b478'; ctx.beginPath(); ctx.arc(light.x, light.y, 1.8, 0, Math.PI * 2); ctx.fill()
  }
}
