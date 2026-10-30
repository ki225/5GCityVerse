import type { CityObject, Ue } from '../../domain/model'
import type { ObjectPainter, PaintContext } from '../geometry'
import { colors } from '../../../theme/colors'

export const ueColors: Record<Ue['demand'], string> = colors.map.ue
type ActorBatchPainter = (objects: readonly CityObject[], context: PaintContext) => void
const batchPainters = new WeakMap<ObjectPainter, ActorBatchPainter>()
export function getActorBatchPainter(painter: ObjectPainter | undefined) { return painter ? batchPainters.get(painter) : undefined }

function actorSize(object: CityObject, scale: number, demand: Ue['demand']) {
  const isVehicle = demand === 'realtime_control' || demand === 'connected_traffic'
  const height = Math.round(Math.max(isVehicle ? 10 : 9, Math.min(32, scale * Math.max(object.size.height * 3, object.size.width * 2.6))))
  return { height, width: Math.round(height * (isVehicle ? 1.55 : demand === 'factory_iot' ? .72 : .62)) }
}

/** Tiny reusable actor sprites keep thousands of recognizable people off the DOM. */
export function createActorPainters(): Map<string, ObjectPainter> {
  const cache = new Map<string, HTMLCanvasElement>()
  const painters = new Map<string, ObjectPainter>()
  for (const demand of Object.keys(ueColors) as Ue['demand'][]) {
    const painter: ObjectPainter = (object, { ctx, projection }) => {
    const point = projection.project(object.position, .015)
    const { width, height } = actorSize(object, projection.scale, demand)
    const key = `${demand}:${width}:${height}`
    let sprite = cache.get(key)
    if (!sprite) {
      sprite = document.createElement('canvas'); sprite.width = (width + 4) * 2; sprite.height = (height + 4) * 2
      const c = sprite.getContext('2d')!; c.scale(2, 2); c.translate(2, 2)
      drawActor(c, demand, width, height)
      if (cache.size >= 64) cache.delete(cache.keys().next().value!)
      cache.set(key, sprite)
    }
    ctx.drawImage(sprite, point.x - width / 2 - 2, point.y - height - 2, width + 4, height + 4)
    }
    painters.set(`ue:${demand}`, painter)
    if (demand === 'web_browsing' || demand === 'video_streaming') batchPainters.set(painter, (objects, context) => paintPeopleBatch(objects, context, demand))
  }
  return painters
}

/** Batch consecutive people between occluders. Shape remains a person at high density. */
function paintPeopleBatch(objects: readonly CityObject[], { ctx, projection }: PaintContext, demand: 'web_browsing' | 'video_streaming') {
  const legs = new Path2D(), bodies = new Path2D(), heads = new Path2D(), hair = new Path2D()
  for (const object of objects) {
    const point = projection.project(object.position, .015), { width: w, height: h } = actorSize(object, projection.scale, demand)
    const x = point.x - w / 2, y = point.y - h
    legs.rect(x + w * .24, y + h * .68, w * .18, h * .28); legs.rect(x + w * .58, y + h * .68, w * .18, h * .28)
    bodies.roundRect(x + w * .15, y + h * .3, w * .7, h * .43, 1)
    for (const side of [-1, 1]) {
      const root = x + w * (side === -1 ? .18 : .82), tip = x + w * (side === -1 ? .04 : .96), armY = y + h * (demand === 'video_streaming' ? .16 : .65), thickness = w * .12
      bodies.moveTo(root - thickness, y + h * .4); bodies.lineTo(tip - thickness, armY); bodies.lineTo(tip + thickness, armY); bodies.lineTo(root + thickness, y + h * .43); bodies.closePath()
    }
    heads.moveTo(x + w / 2 + h * .16, y + h * .19); heads.arc(x + w / 2, y + h * .19, h * .16, 0, Math.PI * 2)
    hair.moveTo(x + w / 2 - h * .15, y + h * .1); hair.ellipse(x + w / 2, y + h * .1, h * .155, h * .07, 0, Math.PI, Math.PI * 2)
  }
  ctx.fillStyle = '#355257'; ctx.fill(legs)
  ctx.fillStyle = ueColors[demand]; ctx.fill(bodies)
  ctx.fillStyle = '#e4c8a3'; ctx.fill(heads)
  ctx.fillStyle = '#51635c'; ctx.fill(hair)
}

function drawActor(ctx: CanvasRenderingContext2D, demand: Ue['demand'], width: number, height: number) {
  const body = ueColors[demand]
  ctx.fillStyle = '#475d4833'; ctx.beginPath(); ctx.ellipse(width / 2, height - .5, width * .46, 1.4, 0, 0, Math.PI * 2); ctx.fill()
  if (demand === 'web_browsing' || demand === 'video_streaming') {
    ctx.fillStyle = '#355257'; ctx.fillRect(width * .24, height * .68, width * .18, height * .28); ctx.fillRect(width * .58, height * .68, width * .18, height * .28)
    ctx.fillStyle = body; ctx.beginPath(); ctx.roundRect(width * .15, height * .3, width * .7, height * .43, [1.5, 1.5, 1, 1]); ctx.fill()
    ctx.fillStyle = '#e4c8a3'; ctx.beginPath(); ctx.arc(width / 2, height * .19, height * .16, 0, Math.PI * 2); ctx.fill()
    ctx.fillStyle = '#51635c'; ctx.beginPath(); ctx.ellipse(width / 2, height * .1, height * .155, height * .07, -.1, Math.PI, Math.PI * 2); ctx.fill()
    ctx.strokeStyle = body; ctx.lineWidth = Math.max(1, width * .16); ctx.lineCap = 'round'; ctx.beginPath()
    if (demand === 'video_streaming') { ctx.moveTo(width * .18, height * .4); ctx.lineTo(0, height * .18); ctx.moveTo(width * .82, height * .4); ctx.lineTo(width, height * .13) }
    else { ctx.moveTo(width * .18, height * .4); ctx.lineTo(width * .08, height * .66); ctx.moveTo(width * .82, height * .4); ctx.lineTo(width * .92, height * .66) }
    ctx.stroke()
  } else if (demand === 'factory_iot') {
    ctx.fillStyle = '#6b7d64'; ctx.fillRect(width * .45, height * .5, width * .13, height * .45)
    ctx.fillStyle = '#a9bd8d'; ctx.fillRect(width * .15, height * .3, width * .73, height * .35)
    ctx.fillStyle = '#486d57'; ctx.fillRect(width * .31, height * .39, width * .4, height * .12)
    ctx.strokeStyle = '#718a45'; ctx.lineWidth = .8; ctx.beginPath(); ctx.arc(width / 2, height * .28, height * .2, Math.PI * 1.15, Math.PI * 1.85); ctx.stroke()
  } else {
    const realtime_control = demand === 'realtime_control'
    ctx.fillStyle = '#36515a'; ctx.beginPath(); ctx.arc(width * .25, height * .85, height * .14, 0, Math.PI * 2); ctx.arc(width * .77, height * .85, height * .14, 0, Math.PI * 2); ctx.fill()
    ctx.fillStyle = realtime_control ? '#f5efe0' : body; ctx.beginPath(); ctx.roundRect(0, height * .35, width, height * .48, 1.5); ctx.fill()
    ctx.fillStyle = realtime_control ? '#dce2d9' : '#a7a4c3'; ctx.beginPath(); ctx.roundRect(width * .2, height * .12, width * .56, height * .38, 1.5); ctx.fill()
    ctx.fillStyle = '#719aa7'; ctx.fillRect(width * .58, height * .21, width * .16, height * .22)
    if (realtime_control) {
      ctx.fillStyle = '#ca6a65'; ctx.fillRect(width * .27, height * .42, width * .09, height * .28); ctx.fillRect(width * .21, height * .51, width * .21, height * .09)
      ctx.fillStyle = '#ce7772'; ctx.fillRect(width * .35, height * .03, width * .22, height * .09)
    } else { ctx.fillRect(width * .29, height * .21, width * .2, height * .22) }
    ctx.fillStyle = '#e9d9a1'; ctx.fillRect(width * .93, height * .48, width * .06, height * .12)
  }
}
