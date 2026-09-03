import { expect, test } from "@playwright/test";
import { seedModel, seedProvider } from "./helpers";

test("UE-21-1 新建：列表出现且 Key 只显示尾 4 位掩码", async ({ page }) => {
  await page.goto("/providers");
  await page.getByRole("button", { name: "新建 Provider" }).click();
  await page.getByLabel("名称").fill("DeepSeek E2E");
  await page.getByLabel("协议").selectOption("openai_compatible");
  await page.getByLabel("Base URL").fill("http://127.0.0.1:9801/v1");
  await page.getByLabel("API Key").fill("sk-e2e-abcd9999");
  await page.getByRole("button", { name: "保存" }).click();

  await expect(page.getByText("DeepSeek E2E")).toBeVisible();
  await expect(page.getByText("****9999")).toBeVisible();
  expect(await page.content()).not.toContain("sk-e2e-abcd9999"); // 明文不出现
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

test("UE-21-3 删除保护：挂载 Model 的 Provider 删除显示 409 提示，不白屏", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-删除保护");
  await seedModel(request, providerId, "占位模型");

  await page.goto("/providers");
  await page.locator("tr", { hasText: "E2E-删除保护" }).getByRole("button", { name: "删除" }).click();
  await expect(page.getByRole("alert")).toContainText("模型");
  await expect(page.getByRole("button", { name: "新建 Provider" })).toBeVisible(); // 页面仍正常
});
