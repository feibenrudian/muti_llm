import { expect, test } from "@playwright/test";
import { seedModel, seedProvider } from "./helpers";

test("UE-23-1 新建 Pipeline 全流程：2 成员排序 + 裁判 + 默认模板，保存后回显", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-管道");
  const m1 = await seedModel(request, providerId, "m-one");
  const m2 = await seedModel(request, providerId, "m-two");

  await page.goto("/pipelines");
  await page.getByRole("button", { name: "新建 Pipeline" }).click();
  await page.getByLabel("名称（对外虚拟模型名）").fill("council-e2e");
  await page.getByLabel("裁判模型").selectOption(String(m1));

  // 成员区：初始 1 行选 m-two，添加第 2 行选 m-one（保存后回显顺序 m-two、m-one）
  const memberSelects = page.locator(".space-y-2 select");
  await memberSelects.nth(0).selectOption(String(m2));
  await page.getByRole("button", { name: "+ 添加成员" }).click();
  await memberSelects.nth(1).selectOption(String(m1));

  await page.getByRole("button", { name: "保存" }).click();
  await expect(page.locator("tr", { hasText: "council-e2e" })).toBeVisible();
  await expect(page.locator("tr", { hasText: "council-e2e" })).toContainText("m-two、m-one");
});

test("UE-23-2 校验：不选成员或裁判被拦截", async ({ page, request }) => {
  await seedProvider(request, "E2E-校验管道");
  await page.goto("/pipelines");
  await page.getByRole("button", { name: "新建 Pipeline" }).click();
  await page.getByLabel("名称（对外虚拟模型名）").fill("bad-pipeline");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(page.getByRole("alert")).toContainText(/成员|裁判/);
  await expect(page.locator("tr", { hasText: "bad-pipeline" })).toHaveCount(0);
});

test("UE-23-3 模板编辑：修改可保存、恢复默认按钮生效", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-模板");
  const m1 = await seedModel(request, providerId, "tpl-m");
  await page.goto("/pipelines");
  await page.getByRole("button", { name: "新建 Pipeline" }).click();
  await page.getByLabel("名称（对外虚拟模型名）").fill("tpl-pipeline");
  await page.getByLabel("裁判模型").selectOption(String(m1));
  await page.locator(".space-y-2 select").nth(0).selectOption(String(m1));

  const textarea = page.getByLabel("裁判 Prompt 模板");
  // 默认模板已加载（异步），等待出现占位符
  await expect(textarea).toContainText("{{original_messages}}", { timeout: 10_000 });
  await textarea.fill("自定义模板 {{candidate_answers}}");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(page.locator("tr", { hasText: "tpl-pipeline" })).toBeVisible();

  // 重新进入编辑回显一致
  await page.goto("/pipelines");
  await page.locator("tr", { hasText: "tpl-pipeline" }).getByRole("button", { name: "编辑" }).click();
  await expect(page.getByLabel("裁判 Prompt 模板")).toHaveValue("自定义模板 {{candidate_answers}}");

  // 恢复默认
  await page.getByRole("button", { name: "恢复默认模板" }).click();
  await expect(page.getByLabel("裁判 Prompt 模板")).toContainText("{{original_messages}}");
});
