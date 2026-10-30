import { clickCityObject } from '../helpers/city-map.mjs'
/** Real desktop Chrome geometry; no emulated viewport or intercepted API calls. */
import { chromium, expect } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
const out = new URL('../../../artifacts/city-native-viewport-20261029/', import.meta.url)
await mkdir(out, { recursive: true })
const browser = await chromium.launch({ channel: 'chrome', headless: false, args: ['--start-maximized'] })
try {
  const context = await browser.newContext({ viewport: null })
  const page = await context.newPage()
  await page.goto(process.env.CITYVERSE_INTEGRATION_URL || 'http://127.0.0.1:5173')
  await expect(page.getByTestId('city-network-map')).toBeVisible()
  const cdp = await context.newCDPSession(page)
  const { windowId } = await cdp.send('Browser.getWindowForTarget')
  const results = []
  async function check(name) {
    const inspector = page.getByTestId('city-map-inspector')
    if (await inspector.getAttribute('aria-hidden') === 'false') {
      await expect(inspector).toHaveCSS('opacity', '1')
      await expect(inspector).toHaveCSS('transform', 'matrix(1, 0, 0, 1, 0, 0)')
    }
    const geometry = await page.evaluate(() => ({
      inner: { width: innerWidth, height: innerHeight },
      outer: { width: outerWidth, height: outerHeight },
      screen: { availableWidth: screen.availWidth, availableHeight: screen.availHeight },
      document: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
      panels: [...document.querySelectorAll('[data-testid="city-network-map"],[aria-label="四步實驗流程"]')].map(e => ({ name: e.dataset.testid || e.getAttribute('aria-label'), top: e.getBoundingClientRect().top, bottom: e.getBoundingClientRect().bottom })),
    }))
    expect(geometry.document.height).toBeLessThanOrEqual(geometry.inner.height + 1)
    expect(geometry.document.width).toBeLessThanOrEqual(geometry.inner.width + 1)
    expect(geometry.outer.height).toBeLessThanOrEqual(geometry.screen.availableHeight + 16)
    for (const panel of geometry.panels) {
      expect(panel.top).toBeGreaterThanOrEqual(0)
      expect(panel.bottom).toBeLessThanOrEqual(geometry.inner.height - 4)
    }
    results.push({ name, ...geometry })
    await page.screenshot({ path: new URL(name + '.png', out).pathname.replace(/^\/([A-Za-z]:)/, '$1'), scale: 'css' })
  }
  await clickCityObject(page, 'distributed')
  await expect(page.getByTestId('city-map-inspector')).toHaveCSS('opacity', '1')
  await check('native-maximized-infra')
  await clickCityObject(page, 'arena')
  await expect(page.getByTestId('city-demand-card')).toBeVisible()
  await check('native-maximized-building')
  const available = await page.evaluate(() => ({ width: screen.availWidth, height: screen.availHeight }))
  await cdp.send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'normal' } })
  await cdp.send('Browser.setWindowBounds', { windowId, bounds: { left: 0, top: 0, width: Math.min(1100, available.width), height: Math.min(550, available.height) } })
  await expect.poll(() => page.evaluate(() => innerWidth)).toBeLessThan(available.width)
  await check('native-resized-building')
  await page.keyboard.press('Escape')
  await page.getByTestId('city-draft-only').locator('summary').click()
  await check('native-resized-expanded-notice')
  await writeFile(new URL('native-browser-result.json', out), JSON.stringify({ source: 'Real Chrome windows with viewport:null, no HTTP/WS interception', results }, null, 2))
  process.stdout.write('Native Chrome geometry passed: maximized and resized windows; borders remain visible.\n')
} finally { await browser.close() }
