# 5GCityVerse frontend

The product uses the on-prem backend, and every startup mode serves a production build. `dev` and `preview` are aliases of the same production startup flow; there is no separate development API or cloud UI.

The interface remains in Traditional Chinese and opens directly at the access gate or city view.

Opening the page shows the city map, where users can select buildings and set demand. If the backend network is not ready, an expandable status line appears above the map with the reason and network initialization details. Access previews and experiment starts remain disabled, so no UE starts; the draft exists only for the current page session. This is the demand-editing state of the same product and does not switch execution environments or invent network readiness.

## Build and start

With Node.js 22.12 or newer, run these commands from `frontend/`:

```bash
npm ci
npm run build
npm start
```

The browser entry point defaults to `http://127.0.0.1:5173`. The Node server serves `dist/` and proxies `/api`, network observations, and `/ws` to the on-prem backend at `http://127.0.0.1:8000`. Set `CITYVERSE_BACKEND_URL` to change the backend origin, or `PORT` and `FRONTEND_HOST` to change the listen address. The deployment reverse proxy provides external TLS.

The default API and WebSocket connection is same-origin. For a different origin, set `VITE_API_URL` and `VITE_WS_URL` as shown in `.env.example` before building. Every `VITE_` value enters the browser bundle; do not put secrets there.
