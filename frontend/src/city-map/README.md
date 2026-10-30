# City map module

## Directory layout and responsibilities

```text
city-map/
├── CityNetworkMap.tsx       # Product map entry point and interaction integration
├── components/              # Canvas, building demand cards, demand flow, and infrastructure cards
├── context/                 # City demand drafts and formal intent state
├── domain/                  # Map objects, landmarks, demand compilation, and tests
├── rendering/               # Renderer, projection, geometry, resolution, and demand markers
│   └── painters/            # Building, person, landmark, and network-device painters
├── data/                   # City presets and exported infrastructure configuration
├── services/               # Infrastructure reads, response validation, and tests
└── styles/                 # Map, product layout, and demand card styles
```

Tests live beside the code they cover. External consumers import the actual entry point, component, or context path; there are no legacy path shims.

`domain` defines objects and demand data, while `rendering` draws the map. `data/presets.ts` assembles city configuration. `context/CityDemandContext.tsx` manages demand drafts, and `context/FormalIntentContext.tsx` manages formal submissions and retries. Moving folders did not change coordinates, component appearance, event-driven drawing, or API contracts.

## Infrastructure information card

Selecting a DataCenter or gNB on the map opens `InfraCard` in the shared detail area to the map's right. Buildings use the same area. When the card opens, a CSS transform moves the Canvas smoothly left; closing restores it without recreating the renderer. An NF resolves to its data center. The card scrolls independently within the map area without increasing page height.

The close button, Escape, selecting empty space, or unmounting the owning plugin closes the card. Central retains its off-city coordinate. The device list and temporary event entry point are currently hidden; the Canvas no longer draws the fixed Central illustration or its link.

The card has two data sources:

- **Live Pods:** Read-only `GET /api/onprem/city-infra` shows observation time, Pod phase and readiness, Node, IP, container requests and limits, restart count, and image. CPU and memory usage are not yet available; requests must not be presented as actual usage.
- **Configured definitions:** Exported from `infrastructure/onprem/values.yaml` and displayed separately from running state. Edge is currently a reserved site with no configured NF Pod.

The backend prefers Pod site labels. If a valid label is missing, it matches the workload's configured site and labels this fallback in the card. Failed reads are not shown as “zero Pods,” and configured values are not presented as live values. The Pod API does not return environment variables, Secrets, or volume definitions.

## Execution and data sources

Only the main product entry point mounts the map. It uses the production same-origin API proxy in `frontend/server.mjs`; there is no standalone lab entry point or second Vite configuration. Other sources can set `VITE_CITY_INFRA_API_URL` or inject an `InfraSource`.

Reads occur only on selection or manual refresh. Switching or closing cancels pending requests, with a 12-second timeout. Reads do not create a continuous Canvas rendering loop, and moving map objects does not migrate Pods.

Update exported configuration data with:

```sh
python -m backend.onprem.application.city_infra --export-config frontend/src/city-map/data/infra-config.json
```

Verify with `npm test` and `npm run test:e2e` from `frontend/`. See [product startup and formal demand flow](../../README.md).
