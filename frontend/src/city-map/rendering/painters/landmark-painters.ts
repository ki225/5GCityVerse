import type { CityLandmark } from '../../domain/landmarks'
import { facade, gable, ivory, line, polygon, prism, tile } from '../geometry'
import type { ObjectPainter } from '../geometry'

export const landmarkPainter: ObjectPainter = (object, context) => {
  const landmark = object as CityLandmark, { x, y, width: w, height: d } = object.bounds
  const h = Math.max(.1, object.size.height), p = context.projection
  if (landmark.landmarkType === 'railway') {
    tile(context, x, y, w, d, '#adb3a3')
    for (let i = 0; i < 30; i++) {
      const px = x + w * i / 30
      line(context, { x: px, y: y + d * .12 }, { x: px, y: y + d * .88 }, '#7d8c80', 1.5, .02)
    }
    for (const offset of [.28, .72]) line(context, { x, y: y + d * offset }, { x: x + w, y: y + d * offset }, '#e8eadf', 1.5, .04)
  } else if (landmark.landmarkType === 'train') {
    for (let i = 0; i < 3; i++) {
      const car = { x: x + w * i / 3, y, width: w * .31, height: d }
      prism(context, car, h, ['#ecede0', '#54838f', '#77a3ad']); facade(context, car, h, 1, 4, '#a6ced0')
    }
  } else if (landmark.landmarkType === 'crane') {
    for (const u of [.08, .82]) for (const v of [.12, .75]) prism(context, { x: x + w * u, y: y + d * v, width: w * .08, height: d * .12 }, h, ['#dec492', '#ae9564', '#c7ae7b'])
    prism(context, { x, y: y + d * .07, width: w, height: d * .2 }, h * .11, ['#e0c997', '#b39b6f', '#ceb683'], h)
    prism(context, { x: x - w * .3, y: y + d * .36, width: w * 1.6, height: d * .12 }, h * .09, ['#e0c997', '#b39b6f', '#ceb683'], h * 1.02)
    line(context, { x: x + w * 1.15, y: y + d * .42 }, { x: x + w * 1.15, y: y + d * .42 }, '#8d8d71', 1, h * .2, h)
  } else {
    const hull = [p.project({ x, y: y + d * .5 }), p.project({ x: x + w * .15, y }), p.project({ x: x + w * .84, y }), p.project({ x: x + w, y: y + d * .5 }), p.project({ x: x + w * .84, y: y + d }), p.project({ x: x + w * .15, y: y + d })]
    polygon(context.ctx, hull.map(point => ({ x: point.x, y: point.y + h * p.scale * .3 })), '#42626b'); polygon(context.ctx, hull, '#d5d9c9')
    for (let i = 0; i < 3; i++) prism(context, { x: x + w * (.24 + i * .17), y: y + d * .22, width: w * .15, height: d * .56 }, h * .5, i % 2 ? ['#dbb38c', '#b88f68', '#cba37e'] : ['#83a8a7', '#537c83', '#6d979b'])
    const cabin = { x: x + w * .08, y: y + d * .24, width: w * .15, height: d * .52 }
    prism(context, cabin, h, ivory); facade(context, cabin, h, 1, 2); gable(context, cabin, h, .03)
  }
}
