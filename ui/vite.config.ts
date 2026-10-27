import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Builds into ../web, which the real server serves on :8602: index.html at /, hashed bundles at /assets/*.
// The dev server proxies /api and /samples (and /catalog) to the mock server on :8612 by default.
// API=live proxies to the real backend on :8602 instead; API_TARGET=<url> picks any other origin.
const target = process.env.API_TARGET || (process.env.API === "live" ? "http://127.0.0.1:8602" : "http://127.0.0.1:8612");
const proxy = { target, changeOrigin: true };

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../web", emptyOutDir: true, assetsDir: "assets", chunkSizeWarningLimit: 900 },
  server: {
    port: 5182,
    host: "127.0.0.1",
    strictPort: true,
    proxy: { "/api": proxy, "/samples": proxy, "/catalog": proxy },
  },
});
