import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The dev stack's public origin (devproxy), and the dev server's own origins.
const API_ORIGIN = "http://localhost:8080";
const DEV_SERVER_ORIGINS = new Set(["http://localhost:5173", "http://127.0.0.1:5173"]);

// What the proxy hook uses of http-proxy-3's ProxyServer (an EventEmitter) and of the outgoing request. Their
// own types build on @types/node, which this browser project does not install, so they are written out here.
interface ProxiedRequest {
  getHeader(name: string): unknown;
  setHeader(name: string, value: string): unknown;
}
interface DevProxy {
  on(event: "proxyReq", listener: (proxyReq: ProxiedRequest) => void): unknown;
}

// The api serves this build same-origin (spec §15, §16.6): no inline script or style may be needed.
export default defineConfig({
  plugins: [react()],
  server: {
    // `vite` (the dev server) only; `vite build` is unchanged. The api behind devproxy answers /api. The edge's
    // CSRF check compares Origin with the public origin, so a request from the dev server's own page has its
    // Origin rewritten to that one. Any other Origin, a cross-site page's included, goes through unchanged, and
    // the api refuses it as it would without the proxy. Every other header (X-JH-Request, X-JH-Background, the
    // session cookie) goes through as the browser sent it.
    proxy: {
      "/api": {
        target: API_ORIGIN,
        configure: (server) => {
          const proxy = server as unknown as DevProxy;
          proxy.on("proxyReq", (proxyReq) => {
            const origin = proxyReq.getHeader("origin");
            if (typeof origin === "string" && DEV_SERVER_ORIGINS.has(origin)) {
              proxyReq.setHeader("origin", API_ORIGIN);
            }
          });
        },
      },
    },
  },
  build: {
    // Every asset is a file of its own, never a `data:` URL (the CSP's `font-src 'self'` refuses a data: font),
    // and no modulepreload polyfill: the entry stays the one `<script type="module" src>` of index.html.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    // The vendor code in a few chunks of its own, each under Vite's 500 kB advisory (rolldown's
    // `output.codeSplitting`, Vite 8). They load as `modulepreload` links of the one entry script, which stays
    // the only `<script>` of index.html (check-dist-csp).
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: "react", test: /[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/ },
            { name: "tanstack", test: /[\\/]node_modules[\\/]@tanstack[\\/]/ },
            { name: "i18n", test: /[\\/]node_modules[\\/](i18next|react-i18next)[\\/]/ },
            { name: "radix", test: /[\\/]node_modules[\\/](@radix-ui|@floating-ui)[\\/]/ },
          ],
        },
      },
    },
  },
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    // A fresh worker and jsdom per test file is the isolation these tests want, so the two hints that suggest
    // trading it away (a vm pool, `isolate: false`) are noise; the import and transform hints stay on.
    experimental: { diagnostics: { environment: false, isolate: false } },
  },
});
