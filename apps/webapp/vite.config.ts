import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API serves the built files at /moderation/ (apps/api/moderation.py). During
// `npm run dev`, API calls go to the local API.
export default defineConfig({
  base: "/moderation/",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
