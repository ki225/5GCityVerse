import http from 'node:http'
import https from 'node:https'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'
import sirv from 'sirv'

const API_PATH = /^\/(?:api|free5gc|metrics|network|events|health)(?:\/|\?|$)/
export const capabilityRequest = (method, url) => method === 'GET' && /^\/api\/capabilities(?:\?|$)/.test(url ?? '')
/** One production server for local use and deployment; no Vite development runtime. */
export function createFrontendServer({ directory = fileURLToPath(new URL('./dist', import.meta.url)), backend = 'http://127.0.0.1:8000' } = {}) {
  const target = new URL(backend)
  if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password || target.search || target.hash || target.pathname !== '/') throw new Error('Backend must be an HTTP(S) origin')
  const transport = target.protocol === 'https:' ? https : http
  const assets = sirv(directory, { etag: true, setHeaders: (response, pathname) => {
    response.setHeader('Cache-Control', /[/\\]assets[/\\]/.test(pathname) ? 'public,max-age=31536000,immutable' : 'no-cache')
    response.setHeader('X-Content-Type-Options', 'nosniff')
  } })
  const server = http.createServer((request, response) => {
    if (API_PATH.test(request.url ?? '')) {
      const isCapability = capabilityRequest(request.method, request.url)
      let deadline
      const upstream = transport.request(new URL(request.url, target), { method: request.method, headers: { ...request.headers, host: target.host } }, result => {
        response.writeHead(result.statusCode ?? 502, result.headers); result.pipe(response)
      })
      if (isCapability) deadline = setTimeout(() => {
        if (!response.headersSent) { response.writeHead(504, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); response.end('{"error":"AI_CAPABILITIES_TIMEOUT"}') }
        else response.destroy()
        upstream.destroy()
      }, 180000)
      else upstream.setTimeout(35000, () => upstream.destroy())
      response.on('close', () => { clearTimeout(deadline); upstream.destroy() })
      upstream.on('error', () => { if (response.writableEnded) return; if (!response.headersSent) { response.writeHead(502, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); response.end('{"error":"ONPREM_BACKEND_UNAVAILABLE"}') } else response.destroy() })
      request.on('aborted', () => upstream.destroy()); request.pipe(upstream); return
    }
    if (!['GET', 'HEAD'].includes(request.method ?? '')) { response.writeHead(405); response.end(); return }
    assets(request, response, () => {
      if (/\.[A-Za-z0-9]+(?:\?|$)/.test(request.url ?? '')) { response.writeHead(404); response.end(); return }
      request.url = '/index.html'; assets(request, response, () => { response.writeHead(503); response.end('Build the frontend before starting the server.') })
    })
  })
  server.on('upgrade', (request, socket, head) => {
    if (!/^\/ws(?:\?|$)/.test(request.url ?? '')) { socket.destroy(); return }
    const upstream = transport.request(new URL(request.url, target), { headers: { ...request.headers, host: target.host } })
    upstream.on('upgrade', (result, peer, peerHead) => {
      socket.write('HTTP/1.1 101 Switching Protocols\r\n' + Object.entries(result.headers).map(([key, value]) => key + ': ' + value).join('\r\n') + '\r\n\r\n')
      if (peerHead.length) socket.write(peerHead)
      if (head.length) peer.write(head)
      peer.on('error', () => socket.destroy()); socket.on('error', () => peer.destroy())
      peer.on('close', () => socket.destroy()); socket.on('close', () => peer.destroy())
      peer.pipe(socket); socket.pipe(peer)
    })
    upstream.on('response', () => socket.destroy()); upstream.on('error', () => socket.destroy()); upstream.end()
  })
  return server
}
if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const port = Number(process.env.PORT || 5173), host = process.env.FRONTEND_HOST || '127.0.0.1'
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid frontend port')
  const server = createFrontendServer({ backend: process.env.CITYVERSE_BACKEND_URL || 'http://127.0.0.1:8000' })
  server.listen(port, host, () => process.stdout.write('5GCityVerse on-prem: http://' + host + ':' + port + '\n'))
  process.on('SIGINT', () => server.close()); process.on('SIGTERM', () => server.close())
}
