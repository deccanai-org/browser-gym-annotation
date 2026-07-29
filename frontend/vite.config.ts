import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Task Review is a fixed 1440px design; no SSR needed — a plain SPA.
export default defineConfig({
  plugins: [react()],
  server: { port: 5180, host: true},
  preview: { port: 5180 },
});
