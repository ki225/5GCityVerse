import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './test/e2e', workers: 1, timeout: 30000,
  outputDir: '../artifacts/unified-onprem-release/playwright',
  reporter: [['list'], ['json', { outputFile: '../artifacts/unified-onprem-release/browser-tests.json' }]],
  use: { baseURL: 'http://127.0.0.1:5173', browserName: 'chromium',
    ...(process.platform === 'win32' ? { channel: 'msedge' } : {}), screenshot: 'only-on-failure' },
  webServer: { command: 'node server.mjs', url: 'http://127.0.0.1:5173', reuseExistingServer: false },
})
