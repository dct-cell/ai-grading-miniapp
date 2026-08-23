import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * The dev server and the API must look like one site to the browser.
 *
 * The Admin session cookie is `SameSite=Strict` and scoped to `/admin`, so it is
 * only sent on same-site requests. Proxying `/admin` from this dev server to the
 * backend keeps every request on `localhost:5173` from the browser's point of
 * view, which is what makes the cookie ride along.
 *
 * Both ends must use the *same hostname*. `localhost` and `127.0.0.1` are
 * different hosts to a cookie jar even though they resolve to the same machine,
 * so mixing them silently breaks authentication in development only.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    host: "localhost",
    port: 5173,
    proxy: {
      "/admin/api": {
        target: "http://localhost:8000",
        changeOrigin: false,
      },
    },
  },
});
