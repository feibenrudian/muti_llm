import { expect, test } from "@playwright/test";
import { seedModel, seedProvider } from "./helpers";

test("UE-23-1 新建 Pipeline 全流程：2 成员排序 + 裁判 + 默认模板，保存后回显", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-管道");
  const m1 = await seedModel(request, providerId, "m-one");
  const m2 = await seedModel(request, providerId, "m-two");
  // 回归：超长模型名曾把成员 Select 撑出弹窗（flex 子项默认 min-width:auto 不收缩）
  await seedModel(request, providerId, "deepseek-v4-flash-vision-exp-very-long-name");

  await page.goto("/pipelines");
  await page.getByRole("button", { name: "新建 Pipeline" }).click();
  const dialog = page.locator('[data-testid="modal"] > div');
  const dialogBox = await dialog.boundingBox();
  if (!dialogBox) throw new Error("弹窗未渲染");
  for (const sel of await page.locator(".space-y-2 select").all()) {
    const box = await sel.boundingBox();
    if (!box) throw new Error("成员 Select 未渲染");
    expect(box.x).toBeGreaterThanOrEqual(dialogBox.x);
    expect(box.x + box.width).toBeLessThanOrEqual(dialogBox.x + dialogBox.width + 1);
  }
  await page.getByLabel("名称（对外虚拟模型名）").fill("council-e2e");
  // 两级联动：先选供应商，再在该供应商下选模型
  await page.getByLabel("裁判供应商").selectOption(String(providerId));
  await page.getByLabel("裁判模型").selectOption(String(m1));

  // 成员区：初始 1 行选 m-two，添加第 2 行选 m-one（保存后回显顺序 m-two、m-one）
  const memberRow = (nth: number) => page.locator(".space-y-2 > div").nth(nth);
  await memberRow(0).getByLabel("成员1供应商").selectOption(String(providerId));
  await memberRow(0).getByLabel("成员1模型").selectOption(String(m2));
  await page.getByRole("button", { name: "+ 添加成员" }).click();
  await memberRow(1).getByLabel("成员2供应商").selectOption(String(providerId));
  await memberRow(1).getByLabel("成员2模型").selectOption(String(m1));

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
  await page.getByLabel("裁判供应商").selectOption(String(providerId));
  await page.getByLabel("裁判模型").selectOption(String(m1));
  const row0 = page.locator(".space-y-2 > div").nth(0);
  await row0.getByLabel("成员1供应商").selectOption(String(providerId));
  await row0.getByLabel("成员1模型").selectOption(String(m1));

  const textarea = page.getByLabel("裁判 Prompt 模板");
  // 默认指令模板已加载（异步）——新默认为最终指令轮（简短指令，无占位符）
  await expect(textarea).toContainText("最终回答", { timeout: 10_000 });
  await textarea.fill("自定义模板 {{candidate_answers}}");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(page.locator("tr", { hasText: "tpl-pipeline" })).toBeVisible();

  // 重新进入编辑回显一致
  await page.goto("/pipelines");
  await page.locator("tr", { hasText: "tpl-pipeline" }).getByRole("button", { name: "编辑" }).click();
  await expect(page.getByLabel("裁判 Prompt 模板")).toHaveValue("自定义模板 {{candidate_answers}}");

  // 恢复默认
  await page.getByRole("button", { name: "恢复默认模板" }).click();
  await expect(page.getByLabel("裁判 Prompt 模板")).toContainText("最终回答");
});
