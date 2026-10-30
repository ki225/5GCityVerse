import { clickCityObject } from '../helpers/city-map.mjs'
/** Actual onprem ASGI + production proxy smoke. No HTTP/WebSocket interception.
 * Run only against the intentionally write-unconfigured app; never submits live traffic.
 */
import { chromium, expect } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
const base = process.env.CITYVERSE_INTEGRATION_URL || 'http://127.0.0.1:5173'
const out = new URL('../../../artifacts/frontend-backend-integration-20261029/', import.meta.url)
await mkdir(out, { recursive: true })
const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) })
try {
  const page = await browser.newPage({ viewport: { width: 1450, height: 1000 } })
  const errors = [], requests = [], wsFrames = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('response', response => {
    const url = new URL(response.url())
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/free5gc/')) requests.push({ method: response.request().method(), path: url.pathname, status: response.status() })
  })
  page.on('websocket', ws => ws.on('framereceived', frame => {
    try { wsFrames.push(JSON.parse(String(frame.payload)).type) } catch { /* no raw frames in evidence */ }
  }))
  async function read(path) {
    const response = await page.request.get(base + path)
    expect(response.status()).toBe(200)
    return response.json()
  }
  const capabilities = await read('/api/capabilities')
  expect(capabilities.write_configured).toBe(false)
  expect(capabilities.planner_available).toBe(false)
  const profiles = await read('/api/onprem/service-profiles')
  expect(profiles.profileCatalog.profiles.map(row => row.id)).toEqual([
    'web_browsing', 'video_streaming', 'realtime_control', 'connected_traffic', 'factory_iot',
  ])
  expect(profiles.trafficDefaults.video_streaming.offeredDownlinkMbpsPerUe).toBe(4)
  expect(profiles.cityAccessCatalog.gnbs).toHaveLength(7)
  expect(profiles.formalAiEndpoint).toBe('/api/intents')
  const snapshot = await read('/api/onprem/scenario-rounds/current?view=summary')
  expect(snapshot.capabilities.start).toBe(false)
  expect(snapshot.capabilities.admissionMaxUes).toBe(100)
  expect(snapshot.capabilities.verifiedMaxUes).toBe(0)
  expect(snapshot.capabilities.commissioningEnabled).toBe(true)
  const bootstrap = await read('/api/onprem/bootstrap')
  expect(bootstrap.state).not.toBe('ready')
  // The real write boundary rejects before creating an intent or touching UE state.
  const denied = await page.request.post(base + '/api/intents', { data: {}, headers: { 'Idempotency-Key': 'readonly-integration-check' } })
  expect(denied.status()).toBe(503)
  expect((await denied.json()).error).toBe('INTENT_WRITE_NOT_CONFIGURED')
  await page.goto(base)
  await expect(page.getByTestId('city-network-map')).toBeVisible()
  await expect(page.getByTestId('city-draft-only')).toBeVisible()
  await page.getByRole('button', { name: '服務意圖', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.getByText('INTENT_WRITE_NOT_CONFIGURED', { exact: false }).first()).toBeVisible()
  await page.getByRole('button', { name: '關閉服務意圖', exact: true }).click()
  await page.getByTestId('city-draft-only').locator('summary').click()
  await page.getByRole('button', { name: '查看網路初始化狀態', exact: true }).click()
  await expect(page.getByRole('heading', { name: '啟動你的 5G 城市', exact: true })).toBeVisible()
  await expect(page.getByText('Native kubeadm evidence/mutation driver is not configured', { exact: false }).first()).toBeVisible()
  await expect.poll(() => wsFrames.includes('runtime_status')).toBe(true)
  await expect(page.locator('#api-access-token')).toHaveCount(0)
  await expect(page.getByTestId('scenario-submit')).toHaveCount(0)
  await page.getByRole('button', { name: '先編輯城市需求', exact: true }).click()
  await expect(page.getByTestId('city-network-map')).toBeVisible()
  await expect(page.getByTestId('city-draft-only')).toBeVisible()
  await clickCityObject(page, 'home-0')
  const card = page.getByTestId('city-demand-card')
  await card.getByLabel('需求 UE 數量', { exact: true }).fill('60')
  await card.getByRole('button', { name: '儲存需求', exact: true }).click()
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('city-demand-row')).toContainText('60 UE')
  await expect(page.getByTestId('city-demand-preview')).toBeDisabled()
  await page.screenshot({ path: new URL('actual-api-browser.png', out).pathname.replace(/^\/([A-Za-z]:)/, '$1'), fullPage: true })
  expect(errors).toEqual([])
  await writeFile(new URL('http-result.json', out), JSON.stringify({
    source: 'actual onprem ASGI through production frontend proxy; no mocked requests',
    configuration: 'writes and native runtime unconfigured; separate temporary journals',
    liveNativeExecution: false, liveAiExecution: false,
    checks: { httpProxy: true, websocketProxy: true, cityCatalogGnbs: 7, populationAdmission: 100,
      capacityVerified: false, commissioning: true,
      writeConfigured: capabilities.write_configured, plannerAvailable: capabilities.planner_available,
      roundStart: snapshot.capabilities.start, bootstrap: bootstrap.state, rejectedIntentStatus: denied.status(),
      tokenRequired: false, draftEditable: true, draftUes: 60, nativePreviewBlocked: true,
      pageErrors: errors }, requests, wsFrameTypes: wsFrames,
  }, null, 2))
  process.stdout.write('Actual HTTP/WebSocket integration passed; native execution remains blocked.\n')
} finally { await browser.close() }
