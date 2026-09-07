import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 代理目标：开发默认本地后端 8000；e2e 由 Playwright 注入 VITE_PROXY_TARGET 指向 9802
const proxyTarget = process.env.VITE_PROXY_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 开发固定 8001（不占用 vite 默认 5173）；e2e 由 Playwright 以 --port 9803 覆盖
    port: 8001,
    strictPort: true,
    proxy: {
      "/api": proxyTarget,
      "/v1": proxyTarget,
    },
  },
});
