import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: the UI on :5173 proxies /api to the studio backend on :8790.
// Build: output goes into the Python package so `python -m studio` serves it.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8790" } },
  build: { outDir: "../backend/studio/web", emptyOutDir: true, chunkSizeWarningLimit: 1200 },
});
