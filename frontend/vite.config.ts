import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The api serves this build same-origin (spec §15, §16.6): no inline script or style may be needed.
export default defineConfig({
  plugins: [react()],
  test: { include: ["src/**/*.test.ts"] },
});
