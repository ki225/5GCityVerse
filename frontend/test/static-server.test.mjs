import { test } from 'node:test'
import assert from 'node:assert/strict'
import { once } from 'node:events'
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import http from 'node:http'
import { createFrontendServer } from '../server.mjs'

test('production server serves immutable assets and proxies exact onprem requests', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'cityverse-static-'))
  await mkdir(join(directory, 'assets')); await writeFile(join(directory, 'index.html'), '<h1>city</h1>')
  await writeFile(join(directory, 'assets', 'main-123.js'), 'export const city = true')
  let received
  const backend = http.createServer(async (request, response) => {
    let body = ''; for await (const chunk of request) body += chunk
    received = { url: request.url, method: request.method, headers: request.headers, body }
    response.writeHead(202, { 'Content-Type': 'application/json' }); response.end('{"accepted":true}')
  })
  backend.listen(0, '127.0.0.1'); await once(backend, 'listening')
  const frontend = createFrontendServer({ directory, backend: 'http://127.0.0.1:' + backend.address().port })
  frontend.listen(0, '127.0.0.1'); await once(frontend, 'listening')
  const base = 'http://127.0.0.1:' + frontend.address().port
  try {
    const home = await fetch(base + '/'); assert.equal(await home.text(), '<h1>city</h1>')
    assert.equal(home.headers.get('cache-control'), 'no-cache')
    const asset = await fetch(base + '/assets/main-123.js'); assert.equal(asset.status, 200)
    assert.match(asset.headers.get('cache-control'), /immutable/)
    assert.equal((await fetch(base + '/assets/missing.js')).status, 404)
    const result = await fetch(base + '/api/intents?scope=city', { method: 'POST', headers: {
      Authorization: 'Bearer test-only', 'Idempotency-Key': 'same-key', 'X-CityVerse-Session-Id': 'same-owner',
    }, body: '{"text":"city demand"}' })
    assert.equal(result.status, 202); assert.equal(received.url, '/api/intents?scope=city')
    assert.equal(received.headers.authorization, 'Bearer test-only'); assert.equal(received.headers['idempotency-key'], 'same-key')
    assert.equal(received.headers['x-cityverse-session-id'], 'same-owner'); assert.equal(received.body, '{"text":"city demand"}')
    await new Promise(resolve => backend.close(resolve))
    const unavailable = await fetch(base + '/api/capabilities')
    assert.equal(unavailable.status, 502); assert.equal((await unavailable.json()).error, 'ONPREM_BACKEND_UNAVAILABLE')
  } finally {
    frontend.closeAllConnections(); backend.closeAllConnections()
    await new Promise(resolve => frontend.close(resolve)); backend.close()
    await rm(directory, { recursive: true, force: true })
  }
})

test('backend configuration never accepts credentials or a path in its origin', () => {
  for (const backend of ['file:///etc/passwd', 'http://user:secret@localhost', 'http://localhost/api', 'http://localhost?token=x']) {
    assert.throws(() => createFrontendServer({ backend }))
  }
})
