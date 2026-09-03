import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Playwright/开发模式：管理 API 与网关代理到本地后端（e2e 后端固定 9800）
    proxy: {
      "/api": "http://127.0.0.1:9800",
      "/v1": "http://127.0.0.1:9800",
    },
  },
});
