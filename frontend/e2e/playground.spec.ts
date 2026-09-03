import { expect, test } from "@playwright/test";
import { injectSrs, seedCouncilPipeline, seedModel, seedProvider } from "./helpers";

const QUANTUM = "用一句话解释量子纠缠";

test("UE-24-1 完整试运行：成员卡片 + 裁判 Prompt + 最终答案（快照回放）", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-试运行");
  const m1 = await seedModel(request, providerId, "run-m1");
  const m2 = await seedModel(request, providerId, "run-m2");
  await seedCouncilPipeline(request, m1, m2, "council-play");

  await page.goto("/playground");
  await page.getByLabel("Pipeline（虚拟模型）").selectOption("council-play");
  await page.getByLabel("用户消息").fill(QUANTUM);
  await page.getByRole("button", { name: "运行" }).click();

  // 两个成员卡片（各自快照答案）
  await expect(page.getByText("执行链路", { exact: false })).toBeVisible({ timeout: 30_000 });
  const memberCards = page.locator("div", { hasText: /^成员/ }).filter({ hasText: "deepseek-v4-flash" });
  await expect(page.getByText("成员", { exact: true })).toHaveCount(2);
  // 成员答案来自 passthrough_basic / param_merge_09 快照（真实固化文本片段）
  await expect(page.getByText("量子纠缠", { exact: false }).first()).toBeVisible();

  // 裁判 Prompt 含两成员答案（入参区）
  await expect(page.getByText("裁判", { exact: true })).toBeVisible();
  // 最终答案
  await expect(page.getByText("最终答案")).toBeVisible();
  await expect(page.getByText("最终答案").locator("..").getByText("量子纠缠", { exact: false })).toBeVisible();
  await expect(memberCards.first()).toBeVisible();
});

test("UE-24-2 失败可读：全部成员注入失败 → 展示错误与原因，不白屏不挂起", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-试运行失败");
  const m1 = await seedModel(request, providerId, "fail-m1");
  const m2 = await seedModel(request, providerId, "fail-m2");
  await seedCouncilPipeline(request, m1, m2, "council-fail");

  await injectSrs(request, { fail_times: { "deepseek-v4-flash": 20 } });

  await page.goto("/playground");
  await page.getByLabel("Pipeline（虚拟模型）").selectOption("council-fail");
  await page.getByLabel("用户消息").fill(QUANTUM);
  await page.getByRole("button", { name: "运行" }).click();

  await expect(page.getByRole("alert").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/全部成员失败|策略执行失败/).first()).toBeVisible();
  // 页面仍可用（导航可点击，不白屏）
  await page.getByRole("link", { name: "设置" }).click();
  await expect(page.getByText("服务信息")).toBeVisible();
  await injectSrs(request, { reset: true });
});
