import { expect, test, type APIRequestContext } from "@playwright/test";
import { ICE_REFINE_QUESTION, injectSrs, seedIcePipeline, seedModel, seedProvider } from "./helpers";

test.beforeEach(async ({ request }) => {
  await request.post("/api/admin/traces/clear");
  await injectSrs(request, { reset: true }); // 清除其他用例遗留的失败/延迟注入
});

/** 经 playground API 造一条 ICE trace（消息与录制场景逐字节一致才命中快照）。 */
async function runIceTrace(request: APIRequestContext, name: string) {
  const resp = await request.post("/api/admin/playground/run", {
    data: { pipeline_name: name, message: ICE_REFINE_QUESTION },
  });
  return resp.json();
}

test("UE-36-1 建 ICE Pipeline：选 ice 出现参数区（默认值），保存后列表与编辑回显一致", async ({
  page,
  request,
}) => {
  const providerId = await seedProvider(request, "E2E-ICE-建管");
  const m1 = await seedModel(request, providerId, "ice-f1");
  const m2 = await seedModel(request, providerId, "ice-f2");

  await page.goto("/pipelines");
  await page.getByRole("button", { name: "新建 Pipeline" }).click();
  // 默认 council：不显示参数区
  await expect(page.getByTestId("ice-params")).toHaveCount(0);

  await page.getByLabel("名称（对外虚拟模型名）").fill("ice-e2e");
  await page.getByLabel("聚合策略").selectOption("ice");
  // 参数区出现且为默认值（max_rounds=3 / threshold=0.8 / 两个开关默认开）
  await expect(page.getByTestId("ice-params")).toBeVisible();
  await expect(page.getByLabel(/max_rounds/)).toHaveValue("3");
  await expect(page.getByLabel(/confidence_threshold/)).toHaveValue("0.8");
  await expect(page.getByLabel(/stagnation/)).toBeChecked();
  await expect(page.getByLabel(/progress_comments/)).toBeChecked();

  // 切换策略联动重置参数区：ice→council 参数区消失，再切回 ice 恢复默认值
  await page.getByLabel(/max_rounds/).fill("2");
  await page.getByLabel("聚合策略").selectOption("council");
  await expect(page.getByTestId("ice-params")).toHaveCount(0);
  await page.getByLabel("聚合策略").selectOption("ice");
  await expect(page.getByLabel(/max_rounds/)).toHaveValue("3");
  await page.getByLabel(/max_rounds/).fill("2");

  await page.getByLabel("裁判供应商").selectOption(String(providerId));
  await page.getByLabel("裁判模型").selectOption(String(m1));
  await page.getByLabel("成员1供应商").selectOption(String(providerId));
  await page.getByLabel("成员1模型").selectOption(String(m1));
  await page.getByRole("button", { name: "+ 添加成员" }).click();
  await page.getByLabel("成员2供应商").selectOption(String(providerId));
  await page.getByLabel("成员2模型").selectOption(String(m2));

  await page.getByRole("button", { name: "保存" }).click();
  const row = page.locator("tr", { hasText: "ice-e2e" });
  await expect(row).toBeVisible();
  await expect(row).toContainText("ice");

  // 编辑回显：策略与 strategy_params 与保存一致
  await row.getByRole("button", { name: "编辑" }).click();
  await expect(page.getByLabel("聚合策略")).toHaveValue("ice");
  await expect(page.getByLabel(/max_rounds/)).toHaveValue("2");
  await expect(page.getByLabel(/confidence_threshold/)).toHaveValue("0.8");
  await expect(page.getByLabel(/stagnation/)).toBeChecked();
});

test("UE-36-2 参数校验：max_rounds=6 前端拦截；绕过前端后端 422 提示可读", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-ICE-校验");
  const m1 = await seedModel(request, providerId, "ice-badm");

  await page.goto("/pipelines");
  await page.getByRole("button", { name: "新建 Pipeline" }).click();
  await page.getByLabel("名称（对外虚拟模型名）").fill("ice-bad");
  await page.getByLabel("聚合策略").selectOption("ice");
  await page.getByLabel(/max_rounds/).fill("6");
  await page.getByLabel("裁判供应商").selectOption(String(providerId));
  await page.getByLabel("裁判模型").selectOption(String(m1));
  await page.getByLabel("成员1供应商").selectOption(String(providerId));
  await page.getByLabel("成员1模型").selectOption(String(m1));
  await page.getByRole("button", { name: "保存" }).click();
  // 前端拦截：错误可读且未落库
  await expect(page.getByRole("alert")).toContainText("max_rounds 必须是 1-5 的整数");
  await expect(page.locator("tr", { hasText: "ice-bad" })).toHaveCount(0);

  // 绕过前端直连 API：后端 422 且 detail 可读
  const resp = await request.post("/api/admin/pipelines", {
    data: {
      name: "ice-bad-api",
      strategy: "ice",
      judge_model_id: m1,
      members: [{ model_id: m1 }],
      strategy_params: { max_rounds: 6 },
    },
  });
  expect(resp.status()).toBe(422);
  expect(JSON.stringify(await resp.json())).toContain("max_rounds");
  await expect(page.locator("tr", { hasText: "ice-bad-api" })).toHaveCount(0);
});

test("UE-36-3 Trace 轮次展示：成员卡带第 0/1 轮标签，裁判卡内多轮评论折叠块逐轮可展开", async ({
  page,
  request,
}) => {
  const providerId = await seedProvider(request, "E2E-ICE-日志");
  const m1 = await seedModel(request, providerId, "ice-t1");
  const m2 = await seedModel(request, providerId, "ice-t2");
  await seedIcePipeline(request, m1, m2, "ice-trace");

  const result = await runIceTrace(request, "ice-trace");
  expect(result.status_code).toBe(200);

  await page.goto(`/traces/${result.trace_id}`);
  // 成员卡按轮分组：第 0/1 轮各占一个页面 tab，卡片带轮次标签
  await page.getByRole("tab", { name: "成员模型输出 · 第 0 轮" }).click();
  const memberCard = page.locator("section", { hasText: "成员调用（2 个）" });
  await expect(memberCard.getByText("第 0 轮", { exact: true })).toBeVisible();
  await expect(memberCard.getByRole("tab")).toHaveCount(2);
  await page.getByRole("tab", { name: "成员模型输出 · 第 1 轮" }).click();
  await expect(memberCard.getByText("第 1 轮", { exact: true })).toBeVisible();

  // 裁判卡：两轮评论折叠块默认折叠，逐轮独立展开；终局答案直出
  await page.getByRole("tab", { name: "裁判模型输出" }).click();
  const judgeCard = page.locator("section").filter({ has: page.getByLabel("裁判模型") });
  const round0 = judgeCard.locator("details").filter({ hasText: "第 0 轮 · 评论" });
  const round1 = judgeCard.locator("details").filter({ hasText: "第 1 轮 · 评论" });
  await expect(round0).toBeVisible();
  await expect(round1).toBeVisible();
  await expect(round0).not.toHaveAttribute("open");
  await expect(round1).not.toHaveAttribute("open");
  await round0.locator("summary").first().click();
  await expect(round0.locator("p")).toContainText("consensus");
  await expect(round1).not.toHaveAttribute("open");
  await round1.locator("summary").first().click();
  await expect(round1.locator("p")).toContainText("consensus");
  await expect(judgeCard.locator("p.text-sm")).toContainText("井栏");
});

test("UE-36-4 Playground：ICE pipeline 试运行，多轮成员卡 + 最终答案正常展示", async ({
  page,
  request,
}) => {
  const providerId = await seedProvider(request, "E2E-ICE-试运行");
  const m1 = await seedModel(request, providerId, "ice-p1");
  const m2 = await seedModel(request, providerId, "ice-p2");
  await seedIcePipeline(request, m1, m2, "ice-play");

  await page.goto("/playground");
  await page.getByLabel("Pipeline（虚拟模型）").selectOption("ice-play");
  await page.getByLabel("用户消息").fill(ICE_REFINE_QUESTION);
  await page.getByRole("button", { name: "运行" }).click();

  await expect(page.getByText("执行链路", { exact: false })).toBeVisible({ timeout: 30_000 });
  // 两轮 × 两成员 = 4 张成员卡，轮次标签覆盖成员与评论行（第 0/1 轮各 3 行）
  await expect(page.getByText("成员", { exact: true })).toHaveCount(4);
  await expect(page.getByText("第 0 轮", { exact: true })).toHaveCount(3);
  await expect(page.getByText("第 1 轮", { exact: true })).toHaveCount(3);
  // 最终答案正常展示（快照终局文本）
  await expect(page.getByText("最终答案（success）")).toBeVisible();
  await expect(
    page.getByText("最终答案（success）").locator("..").getByText("井栏", { exact: false }),
  ).toBeVisible();
});

test("UE-36-5 换裁判重跑：ICE trace 换成员模型作裁判，流式两段输出（评论+最终）实时呈现", async ({
  page,
  request,
}) => {
  const providerId = await seedProvider(request, "E2E-ICE-重跑");
  const m1 = await seedModel(request, providerId, "ice-r1");
  const m2 = await seedModel(request, providerId, "ice-r2");
  await seedIcePipeline(request, m1, m2, "ice-rejudge");

  const result = await runIceTrace(request, "ice-rejudge");
  expect(result.status_code).toBe(200);

  await page.goto(`/traces/${result.trace_id}`);
  await page.getByRole("tab", { name: "裁判模型输出" }).click();
  const judgeCard = page.locator("section").filter({ has: page.getByLabel("裁判模型") });
  // 原始 ICE 版本带两轮评论
  await expect(judgeCard.getByText("第 0 轮 · 评论")).toBeVisible();
  await expect(judgeCard.getByText("第 1 轮 · 评论")).toBeVisible();

  // 换到成员 m2 作裁判：空态 → 点生成发起两段式流式重跑
  const tabs = judgeCard.getByRole("tab");
  await expect(tabs).toHaveCount(2);
  await tabs.nth(1).click();
  await expect(judgeCard.getByText("该模型尚未生成裁判结果")).toBeVisible();
  await judgeCard.getByRole("button", { name: "生成", exact: true }).click();

  // 两段输出完整呈现：评论段（自由文本）+ 最终答案，版本数 +1
  await expect(page.getByText("裁判聚合（2 个版本）")).toBeVisible({ timeout: 30_000 });
  const critiqueBlock = judgeCard.locator("details").filter({ hasText: "第一次调用 · 评论" });
  await expect(critiqueBlock.locator("p")).toContainText("【回答 1】");
  await expect(judgeCard.locator("p.text-sm")).toContainText("三种主要说法");
});
