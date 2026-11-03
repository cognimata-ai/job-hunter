import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The api serves this build same-origin (spec §15, §16.6): no inline script or style may be needed.
export default defineConfig({
  plugins: [react()],
  server: {
    // `vite` (the dev server) only; `vite build` is unchanged. The api behind devproxy answers /api. The edge's
    // CSRF check compares Origin with the public origin, so the proxy sends that one; every other header
    // (X-JH-Request, X-JH-Background, the session cookie) goes through as the browser sent it.
    proxy: {
      "/api": { target: "http://localhost:8080", headers: { origin: "http://localhost:8080" } },
    },
  },
  test: { include: ["src/**/*.test.ts"] },
});
