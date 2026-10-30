import { clickCityObject } from '../helpers/city-map.mjs'
import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
const fixture = JSON.parse(readFileSync(new URL('../fixtures/city.json', import.meta.url), 'utf8'))

async function setup(page, { available = true, uncertain = false, networkAvailable = true, capacity = 5, bootstrapReady = true, zeroFleetReady = false, delayedCatalog = false } = {}) {
  const posts = [], errors = []
  let releaseCatalog
  const catalogReady = new Promise(resolve => { releaseCatalog = resolve })
  page.on('pageerror', error => errors.push(error.message))
  await page.routeWebSocket(/\/ws(?:\?|$)/, ws => ws.onMessage(() => {}))
  await page.route('**/*', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    if (!/^\/(api|free5gc|metrics|network|events|health)(\/|$)/.test(path)) return route.continue()
    let data
    if (path === '/api/onprem/bootstrap') data = zeroFleetReady
      ? { state: 'absent', stage: 'ue', message: 'No existing UE', retryable: true, executionAdmission: {
          schemaVersion: 'cityverse.native-execution-admission/v1', mode: 'baseline-device-reservation',
          state: 'ready_to_start', observedAt: new Date().toISOString(), validUntil: new Date(Date.now() + 5000).toISOString(),
          generation: 2, coreRanReady: true, nativeFleetEmpty: true, profileValid: true,
          existingUePduReady: false, serviceEffectVerified: false } }
      : { state: bootstrapReady ? 'ready' : 'failed', stage: bootstrapReady ? null : 'core', message: 'Explicit UI fixture', retryable: false }
    else if (path === '/api/onprem/service-profiles') {
      if (delayedCatalog) await catalogReady
      data = fixture.services
    }
    else if (path === '/api/capabilities') data = { ...fixture.formalCapabilities, planner_available: available }
    else if (path === '/api/onprem/scenario-rounds/current') data = {
      ...fixture.round, capabilities: { ...fixture.round.capabilities, start: networkAvailable,
        candidateMaxUes: capacity, admissionMaxUes: capacity },
    }
    else if (path === '/api/onprem/scenario-rounds/preview') {
      const body = request.postDataJSON()
      data = { schemaVersion: 'cityverse.service-preview/v1', admissible: true, networkCapacityVerified: false,
        warnings: [], capabilities: fixture.services, resolvedRequirements: null,
        spatialResolution: { topologyRevision: body.topologyRevision,
          spatialRevision: fixture.services.cityAccessCatalog.spatialRevision,
          resolvedGroups: body.demandGroups.map(row => ({ groupId: row.groupId, gnbId: 'gnb-0-0',
            cellId: 'fixture', endpoint: 'gnb-embb', distanceKm: 3.5 })) } }
    } else if (path === '/api/intents' && request.method() === 'POST') {
      posts.push({ path, body: request.postDataJSON(), key: request.headers()['idempotency-key'], authorization: request.headers()['authorization'] })
      if (uncertain && posts.length === 1) return route.abort('connectionfailed')
      return route.fulfill({ status: 202, json: { intent_id: 'city-fixture', operation_id: 'fixture-operation', status: 'queued', traffic_start_mode: 'runtime' } })
    } else if (path === '/api/intents/city-fixture') data = { intent_id: 'city-fixture', status: 'running', latest_operation: { operation_id: 'fixture-operation', stage: 'starting_traffic', status: 'running' } }
    else if (path === '/api/intents') data = { items: [] }
    if (request.method() === 'POST' && path !== '/api/onprem/scenario-rounds/preview') {
      if (path !== '/api/intents') posts.push({ path, body: request.postDataJSON() })
    }
    return route.fulfill({ status: data ? 200 : 503, json: data ?? { error: 'Explicit UI fixture: unavailable' } })
  })
  await page.goto('/')
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-TW')
  await expect(page.getByText('選擇操作語言', { exact: false })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'EN', exact: true })).toHaveCount(0)
  return { posts, errors, releaseCatalog }
}

async function authorize(page) {
  await page.getByText('操作授權', { exact: true }).click()
  await page.getByLabel('操作授權權杖', { exact: true }).fill('fixture-ui-control')
}

async function prepare(page, { authorized = true } = {}) {
  await expect(page.getByTestId('city-network-map')).toBeVisible()
  await page.getByRole('button', { name: '確認城市需求', exact: false }).click()
  await clickCityObject(page, 'home-0')
  const card = page.getByTestId('city-demand-card')
  await card.getByLabel('需求 UE 數量', { exact: true }).fill('2')
  await card.getByLabel('每 UE 上傳 Mbps', { exact: true }).fill('0.5')
  await card.getByLabel('每 UE 下載 Mbps', { exact: true }).fill('1')
  await card.getByRole('button', { name: '儲存需求', exact: true }).click()
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('city-demand-row')).toHaveCount(1)
  await page.getByTestId('city-demand-preview').click()
  await expect(page.getByTestId('city-demand-preview-result')).toContainText('預覽可接受')
  await page.getByRole('button', { name: '下一步：意圖與啟動' }).click()
  await page.locator('#city-service-intent').fill('保護住宅服務 60 秒，依授權資源預算調整。')
  if (authorized) await authorize(page)
}

test('operation authority is explicitly entered, transported, and cleared in memory only', async ({ page }) => {
  const { posts } = await setup(page)
  await prepare(page, { authorized: false })
  await expect(page.getByTestId('scenario-submit')).toBeDisabled()
  await authorize(page)
  const input = page.getByLabel('操作授權權杖', { exact: true })
  await expect(input).toHaveAttribute('type', 'password')
  await expect(page.getByTestId('scenario-submit')).toBeEnabled()
  expect(await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]))).not.toContain('fixture-ui-control')
  await page.getByRole('button', { name: '清除操作授權', exact: true }).click()
  await expect(input).toHaveValue('')
  await expect(page.getByTestId('scenario-submit')).toBeDisabled()
  await input.fill('fixture-ui-control')
  await page.getByTestId('scenario-submit').click()
  await expect.poll(() => posts.length).toBe(1)
  expect(posts[0].authorization).toBe('Bearer fixture-ui-control')
  await page.reload()
  expect(await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]))).not.toContain('fixture-ui-control')
  await prepare(page, { authorized: false })
  await page.getByText('操作授權', { exact: true }).click()
  await expect(page.getByLabel('操作授權權杖', { exact: true })).toHaveValue('')
})

test('fresh native empty-fleet admission enables planning without claiming an existing UE', async ({ page }) => {
  const { posts, errors } = await setup(page, { bootstrapReady: false, zeroFleetReady: true })
  await expect(page.getByTestId('city-device-admission-ready')).toContainText('目前沒有已建立的 UE／PDU')
  await prepare(page)
  await expect(page.getByTestId('scenario-submit')).toBeEnabled()
  expect(posts).toHaveLength(0)
  expect(errors).toEqual([])
})

test('production onprem opens without an access token', async ({ page }) => {
  await setup(page)
  await expect(page.getByTestId('city-network-map')).toBeVisible()
  await expect(page.locator('input[type=password]')).toHaveCount(0)
  expect(await page.evaluate(() => sessionStorage.getItem('5gcityverse.apiAccessToken'))).toBeNull()
})

test('map demand submits once through formal intent with location and SLA', async ({ page }) => {
  const { posts, errors } = await setup(page)
  await prepare(page)
  expect(posts).toHaveLength(0)
  await expect(page.getByTestId('scenario-submit')).toBeEnabled()
  await page.getByTestId('scenario-submit').click()
  await expect.poll(() => posts.length).toBe(1)
  const request = posts[0]
  expect(request.path).toBe('/api/intents')
  expect(request.key).toBeTruthy()
  expect(request.body.text).toContain('60')
  expect(request.body.city_traffic.strategy).toBe('none')
  expect(request.body.city_traffic.demandGroups[0]).toMatchObject({ ueCount: 2, sourceLocation: { buildingId: 'home-0' } })
  expect(request.body.city_traffic.serviceRequirements.services).toBeTruthy()
  expect(errors).toEqual([])
})

test('multiple buildings submit 100 UEs together under the backend population limit', async ({ page }) => {
  const { posts } = await setup(page, { capacity: 100 })
  await expect(page.getByTestId('city-network-map')).toBeVisible()
  for (const [building, count] of [['home-0', '60'], ['home-1', '40']]) {
    await clickCityObject(page, building)
    const card = page.getByTestId('city-demand-card')
    await card.getByLabel('需求 UE 數量', { exact: true }).fill(count)
    await card.getByRole('button', { name: '儲存需求', exact: true }).click()
    await page.keyboard.press('Escape')
  }
  await expect(page.getByTestId('city-demand-row')).toHaveCount(2)
  await page.getByTestId('city-demand-preview').click()
  await expect(page.getByTestId('city-demand-preview-result')).toContainText('預覽可接受')
  await page.getByRole('button', { name: '下一步：意圖與啟動' }).click()
  await page.locator('#city-service-intent').fill('服務兩棟住宅共 100 UE，維持 60 秒並遵守核准的資源預算。')
  await authorize(page)
  await expect(page.getByTestId('scenario-submit')).toBeEnabled()
  await page.getByTestId('scenario-submit').click()
  await expect.poll(() => posts.length).toBe(1)
  const traffic = posts[0].body.city_traffic
  expect(traffic.demandGroups.map(row => row.ueCount)).toEqual([60, 40])
  expect(traffic.schemaVersion).toBe('cityverse.city-traffic/v2')
  expect(traffic).not.toHaveProperty('baselineUes')
  expect(traffic).not.toHaveProperty('events')
  expect(traffic.demandGroups.every(row => row.profileId === 'web_browsing')).toBe(true)
  expect(traffic.strategy).toBe('none')
})

test('old stored language settings do not select a language or block access', async ({ page }) => {
  await page.addInitScript(() => {
    sessionStorage.setItem('5gcityverse.locale', 'en')
    sessionStorage.setItem('5gcityverse.locale.selected', 'false')
  })
  await setup(page)
  await expect(page.getByTestId('city-network-map')).toBeVisible()
  await expect(page.getByRole('button', { name: '確認城市需求', exact: false })).toBeVisible()
})

test('unknown response retries the original payload and key', async ({ page }) => {
  const { posts } = await setup(page, { uncertain: true })
  await prepare(page)
  await page.getByTestId('scenario-submit').click()
  const retry = page.getByRole('button', { name: '核對原始提交', exact: true })
  await expect(retry).toBeVisible()
  await page.locator('#city-service-intent').fill('這是修改後的新草稿，不能取代前次提交。')
  await retry.click()
  await expect.poll(() => posts.length).toBe(2)
  expect(posts[1]).toEqual(posts[0])
  await expect(retry).toHaveCount(0)
})

test('missing formal planner blocks submission after a valid access preview', async ({ page }) => {
  const { posts } = await setup(page, { available: false })
  await prepare(page)
  await expect(page.getByTestId('scenario-submit')).toBeDisabled()
  expect(posts).toHaveLength(0)
})

test('native start admission remains mandatory even when formal AI is configured', async ({ page }) => {
  const { posts } = await setup(page, { networkAvailable: false })
  await prepare(page)
  await expect(page.getByTestId('scenario-submit')).toBeDisabled()
  expect(posts).toHaveLength(0)
})

test('unready backend permits map drafting while preview and native execution remain blocked', async ({ page }) => {
  const { posts, errors } = await setup(page, { bootstrapReady: false, capacity: 100 })
  await expect(page.getByTestId('city-draft-only')).toBeVisible()
  await expect(page.getByTestId('city-network-map')).toBeVisible()
  await clickCityObject(page, 'home-0')
  const card = page.getByTestId('city-demand-card')
  await card.getByLabel('需求 UE 數量', { exact: true }).fill('60')
  await card.getByRole('button', { name: '儲存需求', exact: true }).click()
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('city-demand-row')).toContainText('60 UE')
  await expect(page.getByTestId('city-demand-preview')).toBeDisabled()
  await page.getByRole('button', { name: '下一步：意圖與啟動' }).click()
  await page.locator('#city-service-intent').fill('草稿 60 UE，尚未啟動。')
  await expect(page.getByTestId('scenario-submit')).toBeDisabled()
  expect(posts).toHaveLength(0)
  expect(errors).toEqual([])
})

test('late service settings preserve the edited population and traffic fields', async ({ page }) => {
  const { releaseCatalog, posts } = await setup(page, { bootstrapReady: false, capacity: 100, delayedCatalog: true })
  await clickCityObject(page, 'home-0')
  const card = page.getByTestId('city-demand-card')
  await card.getByLabel('需求 UE 數量', { exact: true }).fill('60')
  await card.getByLabel('每 UE 上傳 Mbps', { exact: true }).fill('0.5')
  releaseCatalog()
  await expect(card.getByRole('button', { name: '儲存需求', exact: true })).toBeEnabled()
  await expect(card.getByLabel('需求 UE 數量', { exact: true })).toHaveValue('60')
  await expect(card.getByLabel('每 UE 上傳 Mbps', { exact: true })).toHaveValue('0.5')
  await expect(card.getByLabel('每 UE 下載 Mbps', { exact: true })).toHaveValue('1')
  await card.getByRole('button', { name: '儲存需求', exact: true }).click()
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('city-demand-row')).toContainText('60 UE')
  expect(posts).toHaveLength(0)
})

test('building-only map hides pickers and event entry while direct building clicks save demand', async ({ page }) => {
  const { posts } = await setup(page)
  await expect(page.getByLabel('檢視設備', { exact: true })).toBeHidden()
  await expect(page.getByLabel('建築需求', { exact: true })).toBeHidden()
  await expect(page.getByRole('button', { name: '新增臨時事件', exact: true })).toHaveCount(0)
  await expect(page.getByText('或按「新增臨時事件」', { exact: false })).toHaveCount(0)
  await clickCityObject(page, 'arena')
  const card = page.getByTestId('city-demand-card')
  await expect(card).toContainText('演唱會場')
  await card.getByLabel('需求 UE 數量', { exact: true }).fill('3')
  await card.getByRole('button', { name: '儲存需求', exact: true }).click()
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('city-demand-row')).toContainText('3 UE')
  expect(posts).toHaveLength(0)
})

test('map and demand controls fit a narrow screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await setup(page)
  await expect(page.getByTestId('city-network-map')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('compact workspace begins below the network notice without redundant header rows', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  await setup(page, { bootstrapReady: false })
  await expect(page.getByTestId('city-network-map')).toBeVisible()
  await expect(page.getByText('On-prem · 城市網路資源管理', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '認識 5GC NF', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '工程資訊', exact: true })).toHaveCount(0)
  await expect(page.getByText('從地圖設定下一輪需求', { exact: true })).toHaveCount(0)
  const notice = await page.getByTestId('city-draft-only').boundingBox()
  const workspace = await page.getByTestId('learning-workspace').boundingBox()
  expect(workspace.y - notice.y - notice.height).toBeLessThanOrEqual(20)
  await expect(page.getByTestId('scenario-lifecycle')).toHaveCount(0)
  await expect(page.getByText('Invalid city demand', { exact: false })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '服務意圖', exact: true })).toHaveCSS('position', 'static')
  await expect.poll(async () => {
    const box = await page.getByTestId('learning-workspace').boundingBox()
    return box.y + box.height
  }).toBeLessThanOrEqual(720)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: '../artifacts/compact-city-layout-20261029/desktop.png', fullPage: true })
})


async function assertViewportBounded(page) {
  const height = await page.evaluate(() => innerHeight)
  for (const locator of [page.getByTestId('city-network-map'), page.getByRole('region', { name: '四步實驗流程', exact: true })]) {
    const box = await locator.boundingBox()
    expect(box.y).toBeGreaterThanOrEqual(0)
    expect(box.y + box.height).toBeLessThanOrEqual(height + 1)
  }
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 1)).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
}

test('infra and building inspectors share the right map card and animate the city left without resizing containers', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 550 })
  const { errors } = await setup(page, { bootstrapReady: false })
  await assertViewportBounded(page)
  const map = page.getByTestId('city-network-map'), canvas = page.locator('canvas')
  const initialMap = await map.boundingBox(), initialCanvas = await canvas.boundingBox()
  await clickCityObject(page, 'distributed')
  const inspector = page.getByTestId('city-map-inspector'), card = page.getByTestId('infra-card')
  await expect(inspector).toHaveCSS('opacity', '1')
  await expect.poll(() => canvas.evaluate(e => new DOMMatrix(getComputedStyle(e).transform).m41)).toBeLessThan(-100)
  const surface = await page.locator('.city-map-surface').boundingBox(), box = await card.boundingBox()
  expect(box.x).toBeGreaterThan(surface.x + surface.width / 3)
  expect(box.y).toBeGreaterThanOrEqual(surface.y)
  expect(box.y + box.height).toBeLessThanOrEqual(surface.y + surface.height + 1)
  expect((await map.boundingBox()).height).toBeCloseTo(initialMap.height, 0)
  expect((await canvas.boundingBox()).height).toBeCloseTo(initialCanvas.height, 0)
  await card.getByRole('button', { name: '配置定義', exact: true }).click()
  await expect(card.getByRole('table').first()).toBeVisible()
  await assertViewportBounded(page)
  await page.screenshot({ path: '../artifacts/city-viewport-inspector-20261029/desktop-infra.png' })
  await clickCityObject(page, 'arena')
  await expect(card).toHaveCount(0)
  await expect(page.getByTestId('city-demand-card')).toBeVisible()
  await page.getByTestId('city-demand-card').getByLabel('需求 UE 數量', { exact: true }).fill('35')
  await assertViewportBounded(page)
  await page.screenshot({ path: '../artifacts/city-viewport-inspector-20261029/desktop-building.png' })
  await page.keyboard.press('Escape')
  await expect.poll(() => canvas.evaluate(e => new DOMMatrix(getComputedStyle(e).transform).m41)).toBe(0)
  await assertViewportBounded(page)
  expect(errors).toEqual([])
})

test('phone keeps both containers inside the viewport and constrains the inspector to the map', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await setup(page, { bootstrapReady: false })
  await assertViewportBounded(page)
  const map = await page.getByTestId('city-network-map').boundingBox()
  const flow = await page.getByRole('region', { name: '四步實驗流程', exact: true }).boundingBox()
  expect(flow.y).toBeGreaterThanOrEqual(map.y + map.height)
  await clickCityObject(page, 'distributed')
  await expect(page.getByTestId('city-map-inspector')).toHaveCSS('opacity', '1')
  const card = await page.getByTestId('infra-card').boundingBox()
  expect(card.y + card.height).toBeLessThanOrEqual(map.y + map.height)
  await page.keyboard.press('Escape')
  await page.getByTestId('city-draft-only').locator('summary').click()
  await assertViewportBounded(page)
  await page.screenshot({ path: '../artifacts/city-viewport-inspector-20261029/phone.png' })
})

test('reduced motion skips the pan transition and quick selection changes keep one current card', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await setup(page)
  const canvas = page.locator('canvas')
  await clickCityObject(page, 'arena')
  await clickCityObject(page, 'distributed')
  await clickCityObject(page, 'hospital')
  await expect(page.getByTestId('infra-card')).toHaveCount(0)
  await expect(page.getByTestId('city-demand-card')).toHaveCount(1)
  await expect(page.getByTestId('city-demand-card')).toContainText('都會醫院')
  await expect(canvas).toHaveCSS('transition-duration', '0s')
  expect(await canvas.evaluate(e => new DOMMatrix(getComputedStyle(e).transform).m41)).toBeLessThan(0)
  await page.keyboard.press('Escape')
  expect(await canvas.evaluate(e => new DOMMatrix(getComputedStyle(e).transform).m41)).toBe(0)
  await assertViewportBounded(page)
})

test('one building retains separate daily browsing and video profile demands', async ({ page }) => {
  const { posts, errors } = await setup(page)
  await clickCityObject(page, 'home-0')
  const card = page.getByTestId('city-demand-card')
  const behavior = card.getByLabel('網路使用行為', { exact: true })
  await expect(behavior.locator('option')).toHaveText(['日常上網', '觀看影音', '即時設備控制', '聯網交通', '工廠 IoT'])
  await card.getByLabel('需求 UE 數量', { exact: true }).fill('2')
  await card.getByRole('button', { name: '儲存需求', exact: true }).click()
  await behavior.selectOption('video_streaming')
  await card.getByLabel('需求 UE 數量', { exact: true }).fill('3')
  await card.getByRole('button', { name: '儲存需求', exact: true }).click()
  await behavior.selectOption('web_browsing')
  await expect(card.getByLabel('需求 UE 數量', { exact: true })).toHaveValue('2')
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('city-demand-row')).toHaveCount(2)
  await page.getByTestId('city-demand-preview').click()
  await expect(page.getByTestId('city-demand-preview-result')).toContainText('預覽可接受')
  await page.getByRole('button', { name: '下一步：意圖與啟動' }).click()
  await page.locator('#city-service-intent').fill('保護這棟住宅的上網與影音需求。')
  await authorize(page)
  await page.getByTestId('scenario-submit').click()
  await expect.poll(() => posts.length).toBe(1)
  const traffic = posts[0].body.city_traffic
  expect(traffic.schemaVersion).toBe('cityverse.city-traffic/v2')
  expect(traffic.demandGroups.map(row => [row.profileId, row.ueCount])).toEqual([['web_browsing', 2], ['video_streaming', 3]])
  expect(new Set(traffic.demandGroups.map(row => row.groupId)).size).toBe(2)
  expect(traffic.demandGroups.every(row => row.sourceLocation.buildingId === 'home-0')).toBe(true)
  expect(Object.keys(traffic.serviceRequirements.services).sort()).toEqual(['video_streaming', 'web_browsing'])
  expect(errors).toEqual([])
})
