import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Task Review is a fixed 1440px design; no SSR needed — a plain SPA.
export default defineConfig({
  plugins: [react()],
  // Proxy /api -> the backend in dev (nginx does this in the container build).
  server: { port: 5180, host: true, proxy: { "/api": "http://localhost:8090" } },
  preview: { port: 5180 },
});
