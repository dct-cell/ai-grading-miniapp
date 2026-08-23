import { defineConfig } from "@playwright/test";

/**
 * End-to-end coverage for the one thing component tests cannot prove: that a
 * *real browser* accepts the session cookie and sends it back.
 *
 * Both ends must use the same hostname. `localhost` and `127.0.0.1` are
 * different hosts to a cookie jar, and the cookie is `SameSite=Strict`, so
 * mixing them breaks authentication in development only — which is exactly the
 * kind of bug this suite exists to catch.
 *
 * Requires `npx playwright install chromium` (a large download) and a running
 * backend on localhost:8000. See admin/README.md.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5173/admin/login",
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
