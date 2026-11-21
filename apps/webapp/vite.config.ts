import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The API serves the built files at /moderation/ (apps/api/moderation.py). During
// `npm run dev`, API calls go to the local API. Component tests run in jsdom.
export default defineConfig({
  base: "/moderation/",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
  test: { environment: "jsdom", include: ["src/**/*.test.tsx"], restoreMocks: true },
});
