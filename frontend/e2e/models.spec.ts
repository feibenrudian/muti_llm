import { expect, test } from "@playwright/test";
import { seedModel, seedProvider } from "./helpers";

test("UE-22-1 新建模型并绑定供应商", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-模型供应商");
  await page.goto("/models");
  await page.getByRole("button", { name: "新建模型" }).click();
  await page.getByLabel("所属供应商").selectOption(String(providerId));
  await page.getByLabel("显示名称").fill("flash-e2e");
  await page.getByLabel("上游模型 ID").fill("deepseek-v4-flash");
  await page.getByLabel("temperature", { exact: false }).fill("0.7");
  await page.getByRole("button", { name: "保存" }).click();

  const row = page.locator("tr", { hasText: "flash-e2e" });
  await expect(row).toBeVisible();
  await expect(row).toContainText("deepseek-v4-flash");
  await expect(row).toContainText('"temperature":0.7');
});

test("UE-22-2 连通性测试：成功显示延迟，坏地址显示失败原因", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-连通性");
  await seedModel(request, providerId, "good-model");
  // 坏地址供应商
  const resp = await request.post("/api/admin/providers", {
    data: { name: "E2E-坏地址", protocol: "openai_compatible", base_url: "http://127.0.0.1:9/v1" },
  });
  const badProviderId = (await resp.json()).id;
  await seedModel(request, badProviderId, "bad-model", "whatever-model");

  await page.goto("/models");
  await page.locator("tr", { hasText: "good-model" }).getByRole("button", { name: "测试" }).click();
  await expect(page.locator("tr", { hasText: "good-model" }).getByText(/成功 \d+ms/)).toBeVisible();
  await expect(page.locator("tr", { hasText: "good-model" }).getByText("pong")).toBeVisible();

  await page.locator("tr", { hasText: "bad-model" }).getByRole("button", { name: "测试" }).click();
  await expect(
    page.locator("tr", { hasText: "bad-model" }).getByText("失败", { exact: true }),
  ).toBeVisible();
  await expect(page.locator("tr", { hasText: "bad-model" }).getByText(/上游/, { exact: false })).toBeVisible();
});

test("UE-22-3 参数表单校验：temperature 超范围被拦截", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-校验");
  await page.goto("/models");
  await page.getByRole("button", { name: "新建模型" }).click();
  await page.getByLabel("所属供应商").selectOption(String(providerId));
  await page.getByLabel("显示名称").fill("invalid-temp");
  await page.getByLabel("上游模型 ID").fill("deepseek-v4-flash");
  await page.getByLabel("temperature", { exact: false }).fill("5");
  await page.getByRole("button", { name: "保存" }).click();

  await expect(page.getByRole("alert")).toContainText("temperature");
  await expect(page.locator("tr", { hasText: "invalid-temp" })).toHaveCount(0); // 未入库
});
