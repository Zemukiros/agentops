import { defineConfig, devices } from "@playwright/test";

// The static recorded demo (ADR-053): built with VITE_DEMO=1 and served by `vite preview`.
// No backend, no tokens, no network beyond the preview server.
export default defineConfig({
  testDir: "./e2e-demo",
  timeout: 180_000,
  expect: { timeout: 30_000 },
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never", outputFolder: "playwright-report-demo" }]] : "list",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "npm run build:demo && npx vite preview --port 4173 --strictPort",
    url: "http://127.0.0.1:4173",
    timeout: 180_000,
    reuseExistingServer: !process.env.CI,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
});
