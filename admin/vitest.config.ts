import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

/**
 * Vitest reads this file rather than the `test` key of `vite.config.ts`, which
 * Vitest 4 no longer merges. Keeping the two separate also means the dev-server
 * proxy config cannot accidentally affect the test environment.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // Playwright drives a real browser and must not be collected by Vitest.
    exclude: ["node_modules/**", "dist/**", "e2e/**"],
  },
});
