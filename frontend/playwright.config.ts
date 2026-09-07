import { defineConfig } from "@playwright/test";

const production = process.env.E2E_MODE === "production";
const backendCmd =
  "cd ../backend && rm -f /tmp/muti_llm_e2e.db && " +
  "MUTILLM_DATABASE_PATH=/tmp/muti_llm_e2e.db uv run uvicorn app.main:app --host 127.0.0.1 --port 9802";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  // 串行执行：共享同一个后端实例，避免用例间数据竞争
  workers: 1,
  fullyParallel: false,
  retries: 0,
  // dev 模式跳过生产冒烟；production 模式只跑生产冒烟
  testIgnore: production ? /^(?!.*production\.spec\.ts).*\.ts$/ : /production\.spec\.ts/,
  use: {
    baseURL: production ? "http://127.0.0.1:9802" : "http://127.0.0.1:9803",
    screenshot: "only-on-failure",
  },
  webServer: production
    ? [{ command: backendCmd, port: 9802, timeout: 60_000, reuseExistingServer: false }]
    : [
        {
          command: "cd ../backend && SRS_DELAY_SCALE=0 uv run python -m tests.srs_runner",
          port: 9801,
          timeout: 60_000,
          reuseExistingServer: false,
        },
        { command: backendCmd, port: 9802, timeout: 60_000, reuseExistingServer: false },
        {
          command: "VITE_PROXY_TARGET=http://127.0.0.1:9802 npm run dev -- --port 9803 --strictPort",
          port: 9803,
          timeout: 60_000,
          reuseExistingServer: false,
        },
      ],
});
