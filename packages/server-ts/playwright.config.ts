/**
 * Playwright configuration for Heurion E2E tests
 */
import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  testMatch: '**/*.spec.ts',
  timeout: 30000,
  expect: { timeout: 10000 },
  retries: 1,
  use: {
    // Nginx serves web dist + proxies /api/ to staging/prod by Host header.
    // On VPS CI, use localhost:80 (Nginx) which routes to staging.heurion.org.
    baseURL: process.env.BASE_URL || 'https://staging.heurion.org',
    headless: true,
    viewport: { width: 1280, height: 800 },
    ignoreHTTPSErrors: true,
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
  ],
})
