import { expect } from '@playwright/test'

// Known preset landmarks. Click actual canvas objects through the public camera.
const targets = {
  'home-0': [2.2, 2.2, .35], 'home-1': [4.5, 2.2, .35],
  arena: [18.5, 4.5, .4], hospital: [4.2, 10.2, .5],
  distributed: [12, 12, .5],
}
export async function clickCityObject(page, id) {
  const point = targets[id]
  if (!point) throw new Error('Unknown map test target: ' + id)
  await page.keyboard.press('Escape')
  const canvas = page.locator('canvas')
  await expect.poll(() => canvas.evaluate(e => new DOMMatrix(getComputedStyle(e).transform).m41)).toBe(0)
  await page.getByRole('button', { name: '重設地圖視角', exact: true }).click()
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  const box = await canvas.boundingBox()
  const scale = Math.max(1, Math.min((box.width - 64) / 48, (box.height - 96) / 24))
  const [x, y, height] = point
  await page.mouse.click(box.x + box.width / 2 + (x - y) * scale,
    box.y + (box.height - 24 * scale) / 2 + 10 + ((x + y) / 2 - height) * scale)
}
