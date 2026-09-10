import { expect, test } from "@playwright/test";
import { seedModel, seedProvider } from "./helpers";

test("UE-21-1 新建：列表出现且 Key 只显示尾 4 位掩码", async ({ page }) => {
  await page.goto("/providers");
  await page.getByRole("button", { name: "新建 Provider" }).click();
  await page.getByLabel("名称").fill("DeepSeek E2E");
  await page.getByLabel("Base URL").fill("http://127.0.0.1:9801/v1");
  await expect(page.locator("form").getByText("已识别协议：openai_compatible")).toBeVisible();
  await page.getByLabel("API Key").fill("sk-e2e-abcd9999");
  await page.getByRole("button", { name: "保存" }).click();

  await expect(page.getByText("DeepSeek E2E")).toBeVisible();
  await expect(page.getByText("****9999")).toBeVisible();
  expect(await page.content()).not.toContain("sk-e2e-abcd9999"); // 明文不出现
});

test("UE-21-5 协议自动识别：Anthropic 官方地址标记 anthropic 并正确入库", async ({ page }) => {
  await page.goto("/providers");
  await page.getByRole("button", { name: "新建 Provider" }).click();
  await page.getByLabel("名称").fill("Claude E2E");
  // hostname 含 "anthropic" 触发协议识别；.invalid 为 RFC 2606 保留 TLD 必不解析，
  // 保存时的连通性探测毫秒级失败（best-effort 秒回），不直连真实 api.anthropic.com
  await page.getByLabel("Base URL").fill("https://api.anthropic.invalid");
  await expect(page.locator("form").getByText("已识别协议：anthropic")).toBeVisible();
  await page.getByLabel("API Key").fill("sk-ant-e2e-12345678");
  await page.getByRole("button", { name: "保存" }).click();

  const row = page.locator("tr", { hasText: "Claude E2E" });
  await expect(row).toBeVisible();
  await expect(row).toContainText("anthropic"); // 协议列
  await expect(row).toContainText("****5678");
});

test("UE-21-2 编辑与启停：改 base_url 生效、停用状态切换", async ({ page, request }) => {
  await seedProvider(request, "E2E-编辑用");
  await page.goto("/providers");
  const row = page.locator("tr", { hasText: "E2E-编辑用" });
  await row.getByRole("button", { name: "编辑" }).click();
  await page.getByLabel("Base URL").fill("http://127.0.0.1:9801/edited");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText("http://127.0.0.1:9801/edited")).toBeVisible();

  const rowAfter = page.locator("tr", { hasText: "E2E-编辑用" });
  await rowAfter.getByRole("button", { name: "停用" }).click();
  await expect(page.locator("tr", { hasText: "E2E-编辑用" }).getByText("停用", { exact: true })).toBeVisible();
  await page.locator("tr", { hasText: "E2E-编辑用" }).getByRole("button", { name: "启用", exact: true }).click();
  await expect(page.locator("tr", { hasText: "E2E-编辑用" }).getByText("启用", { exact: true })).toBeVisible();
});

test("UE-21-3 级联删除：删供应商后其模型一并消失", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-级联删除");
  await seedModel(request, providerId, "占位模型-级联");
  await page.goto("/models");
  await expect(page.locator("tr", { hasText: "占位模型-级联" })).toBeVisible();

  await page.goto("/providers");
  await page.locator("tr", { hasText: "E2E-级联删除" }).getByRole("button", { name: "删除" }).click();
  await expect(page.locator("tr", { hasText: "E2E-级联删除" })).toHaveCount(0);

  await page.goto("/models");
  await expect(page.locator("tr", { hasText: "占位模型-级联" })).toHaveCount(0); // 模型一并删除
  await expect(page.getByRole("button", { name: "新建模型" })).toBeVisible(); // 页面仍正常
});

test("UE-21-6 自动建模型：创建指向 SRS 的供应商后，模型页自动出现上游模型", async ({ page }) => {
  await page.goto("/providers");
  await page.getByRole("button", { name: "新建 Provider" }).click();
  await page.getByLabel("名称").fill("E2E-自动建模型");
  await page.getByLabel("Base URL").fill("http://127.0.0.1:9801/v1");
  await page.getByLabel("API Key").fill("sk-e2e-auto1111");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText("E2E-自动建模型")).toBeVisible();

  await page.goto("/models");
  const row = page.locator("tr", { hasText: "E2E-自动建模型" });
  await expect(row).toBeVisible(); // 供应商列
  await expect(row).toContainText("deepseek-v4-flash"); // 上游模型 ID 列（自动同步）
});

test("UE-21-4 连通性测试：成功显示延迟与模型列表，坏地址显示失败原因", async ({ page, request }) => {
  await seedProvider(request, "E2E-供应商探测");
  // 坏地址供应商
  const resp = await request.post("/api/admin/providers", {
    data: { name: "E2E-坏地址供应商", protocol: "openai_compatible", base_url: "http://127.0.0.1:9/v1" },
  });
  expect(resp.ok()).toBeTruthy();

  await page.goto("/providers");
  await page.locator("tr", { hasText: "E2E-供应商探测" }).getByRole("button", { name: "测试" }).click();
  await expect(page.locator("tr", { hasText: "E2E-供应商探测" }).getByText(/成功 \d+ms/)).toBeVisible();
  await expect(page.locator("tr", { hasText: "E2E-供应商探测" }).getByText("deepseek-v4-flash")).toBeVisible();

  await page.locator("tr", { hasText: "E2E-坏地址供应商" }).getByRole("button", { name: "测试" }).click();
  await expect(page.locator("tr", { hasText: "E2E-坏地址供应商" }).getByText("失败", { exact: true })).toBeVisible();
  await expect(page.locator("tr", { hasText: "E2E-坏地址供应商" }).getByText(/上游/, { exact: false })).toBeVisible();
});
