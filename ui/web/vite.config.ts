import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发期：Vite dev server (5173) 代理到适配器 (8765)；
// 生产：vite build → dist，由 protocol_adapter.py 直接托管。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
    },
  },
  build: { outDir: "dist", chunkSizeWarningLimit: 900 },
});