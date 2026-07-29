import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * Vitest reads THIS file instead of vite.config.ts once it exists, so the react
 * plugin is repeated here: dropping it would silently change how every suite
 * that was already passing gets transformed.
 *
 * jsdom is global rather than per-file because the pure-logic suites are
 * environment-agnostic — they assert on maths, wire formats and rendered markup
 * strings — while LiveBrowserPane and the version-graph components can only be
 * exercised where a real DOM, a real event loop and real pointer dispatch exist.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
});
