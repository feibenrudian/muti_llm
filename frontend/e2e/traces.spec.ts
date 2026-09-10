import { expect, test } from "@playwright/test";
import { injectSrs, seedCouncilPipeline, seedModel, seedProvider } from "./helpers";

const QUANTUM = "用一句话解释量子纠缠";

test.beforeEach(async ({ request }) => {
  await request.post("/api/admin/traces/clear");
  await injectSrs(request, { reset: true }); // 清除其他用例遗留的失败/延迟注入
});

async function makeTrace(request: import("@playwright/test").APIRequestContext, name: string) {
  const resp = await request.post("/api/admin/playground/run", {
    data: { pipeline_name: name, message: QUANTUM },
  });
  return resp.json();
}

test("UE-27-1 列表筛选：status=failed 只显示失败记录", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-日志");
  const m1 = await seedModel(request, providerId, "trace-m1");
  const m2 = await seedModel(request, providerId, "trace-m2");
  await seedCouncilPipeline(request, m1, m2, "council-trace");

  const ok = await makeTrace(request, "council-trace");
  expect(ok.status_code).toBe(200);

  await injectSrs(request, { fail_times: { "deepseek-v4-flash": 20 } });
  const failed = await makeTrace(request, "council-trace");
  expect(failed.status_code).toBe(502);
  await injectSrs(request, { reset: true });

  await page.goto("/traces");
  await expect(page.getByText("共 2 条")).toBeVisible();
  const rows = page.locator("tbody tr");
  await expect(rows).toHaveCount(2);

  await page.getByLabel("状态").selectOption("failed");
  await expect(page.getByText("共 1 条")).toBeVisible();
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("failed");

  // 关键字筛选
  await page.getByLabel("状态").selectOption("");
  await page.getByLabel("关键字（匹配原始输入）").fill("不存在的关键字xyz");
  await expect(page.getByText("没有匹配的记录")).toBeVisible();
});

test("UE-27-2 详情时间线：原始请求 → 裁判 → 成员tab切换 → 最终答案", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-详情");
  const m1 = await seedModel(request, providerId, "detail-m1");
  const m2 = await seedModel(request, providerId, "detail-m2");
  await seedCouncilPipeline(request, m1, m2, "council-detail");

  const result = await makeTrace(request, "council-detail");
  expect(result.status_code).toBe(200);

  await page.goto("/traces");
  await page.locator("tr", { hasText: String(result.trace_id) }).getByRole("link", { name: "详情" }).click();

  await expect(page.getByText("① 原始请求")).toBeVisible();
  await expect(page.getByText("Pipeline：council-detail")).toBeVisible();
  // 成员调用合并为一张 tab 卡片（裁判聚合卡固定置于原始请求之后、成员卡片之前）
  await expect(page.locator("main section h2")).toHaveText([
    "① 原始请求",
    "裁判调用",
    "成员调用（2 个）",
    "Ⓝ 最终响应（success）",
  ]);

  // 页面级 tab：成员组只占一个 tab，点击切到成员卡片（隐藏面板中的元素不参与 role 查询，先切换再断言）
  await page.getByRole("tab", { name: "成员模型输出" }).click();
  await expect(page.getByText("成员调用（2 个）")).toBeVisible();

  const memberCard = page.locator("section").filter({ has: page.getByRole("tablist", { name: "成员模型" }) });
  const memberTabs = memberCard.getByRole("tab");
  await expect(memberTabs).toHaveCount(2);
  await expect(memberTabs.first()).toHaveAttribute("aria-selected", "true");

  // 非选中成员不渲染：入参/输出折叠块只有当前成员一份，且默认折叠保持紧凑
  const outputDetails = memberCard.locator('details:has(summary:text-is("输出"))');
  await expect(outputDetails).toHaveCount(1); // 裁判输出收在轮次折叠块内，成员卡的输出折叠块仅此一份
  await expect(outputDetails).not.toHaveAttribute("open");

  // 切 tab 即换成员模型：入参 temperature 随成员切换（0.7 → 0.9 → 0.7），无需长页滚动
  const inputDetails = memberCard
    .locator("details")
    .filter({ hasText: "入参（实际发出的完整请求）" });
  await inputDetails.locator("summary").click();
  await expect(inputDetails.locator("pre")).toContainText('"temperature": 0.7');
  await memberTabs.nth(1).click();
  await expect(memberTabs.nth(1)).toHaveAttribute("aria-selected", "true");
  await expect(inputDetails.locator("pre")).toContainText('"temperature": 0.9');
  await memberTabs.nth(0).click();
  await expect(inputDetails.locator("pre")).toContainText('"temperature": 0.7');

  // 裁判按轮分层：一级=第 X 轮（第 1 轮评论 / 第 2 轮最终答案），二级=输入/输出，默认全折叠
  await page.getByRole("tab", { name: /裁判/ }).click();
  const judgeCard = page.locator("section", { hasText: "裁判调用" });
  const finalRound = judgeCard
    .locator("details")
    .filter({ has: page.getByText("第 2 轮 · 最终答案", { exact: true }) });
  await finalRound.locator("summary").first().click();
  await finalRound.getByText("输入", { exact: true }).click();
  // 终局输入含组装 Prompt（成员答案标注）
  await expect(finalRound.locator("pre")).toContainText("【回答 1】");
  await finalRound.getByText("输出", { exact: true }).click();
  await page.getByRole("tab", { name: "最终响应" }).click();
  const finalText = await page
    .locator("section", { hasText: "最终响应" })
    .locator("p")
    .innerText();
  await expect(judgeCard.locator("p.text-sm")).toHaveText(finalText);
});

test("UE-27-3 失败详情：成员卡片展示错误信息", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-失败详情");
  const m1 = await seedModel(request, providerId, "fdetail-m1");
  const m2 = await seedModel(request, providerId, "fdetail-m2");
  await seedCouncilPipeline(request, m1, m2, "council-fdetail");

  await injectSrs(request, { fail_times: { "deepseek-v4-flash": 20 } });
  const failed = await makeTrace(request, "council-fdetail");
  expect(failed.status_code).toBe(502);

  await page.goto(`/traces/${failed.trace_id}`);
  await expect(page.getByText("failed", { exact: true }).first()).toBeVisible();
  // 成员卡片展示上游错误信息（注入的失败），且无裁判卡片
  await page.getByRole("tab", { name: /成员/ }).click();
  await expect(page.getByText(/injected failure|上游/).first()).toBeVisible();
  await expect(page.getByText("裁判调用")).toHaveCount(0);
});

test("UE-30-1 换裁判重跑：tab 切换不主动生成，点生成按钮流式输出，可在版本间切换", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-重跑");
  const m1 = await seedModel(request, providerId, "rj-m1");
  const m2 = await seedModel(request, providerId, "rj-m2");
  await seedCouncilPipeline(request, m1, m2, "council-rejudge");

  const result = await makeTrace(request, "council-rejudge");
  expect(result.status_code).toBe(200);

  await page.goto(`/traces/${result.trace_id}`);
  await page.getByRole("tab", { name: /裁判/ }).click();
  const judgeCard = page
    .locator("section")
    .filter({ has: page.getByRole("tablist", { name: "裁判模型" }) });
  const tabs = judgeCard.getByRole("tab");
  // 输出区 = 第二次调用最终答案（p.text-sm）；评论块为 p.text-xs
  const output = judgeCard.locator("p.text-sm");
  const originalText = (await output.innerText()).replace(/\s+/g, " ").trim();

  // tab 默认选中原始裁判（第一个 tab）；tab 仅限本次请求的裁判 + 成员模型
  await expect(tabs).toHaveCount(2);
  await expect(tabs.first()).toHaveAttribute("aria-selected", "true");
  await expect(tabs.first()).toContainText("rj-m1");

  // 切到从未当过裁判的 m2：只显示空态 + 生成按钮，不发起调用
  await tabs.nth(1).click();
  await expect(tabs.nth(1)).toHaveAttribute("aria-selected", "true");
  await expect(judgeCard.getByText("该模型尚未生成裁判结果")).toBeVisible();

  // 注入失败后点生成：流式重跑失败，输出区显示错误
  await injectSrs(request, { fail_times: { "deepseek-v4-flash": 20 } });
  await judgeCard.getByRole("button", { name: "生成", exact: true }).click();
  await expect(judgeCard.getByText(/injected failure|上游|超时/).first()).toBeVisible();

  // 切回原始裁判：立即显示原始答案（不重新调用）
  await injectSrs(request, { reset: true });
  await tabs.nth(0).click();
  await expect(output).toContainText(originalText.slice(0, 30));

  // m2 的失败尝试：展示错误 + 重新生成按钮，点击重新发起并成功，输出实时更新
  await tabs.nth(1).click();
  await judgeCard.getByRole("button", { name: "重新生成" }).click();
  await expect(page.getByText("裁判聚合（2 个版本）")).toBeVisible();
  await expect(output).toContainText(originalText.slice(0, 30)); // 同一快照回放，内容与原始答案一致
  await expect(tabs.nth(1)).toHaveAttribute("aria-selected", "true");

  // 在两个版本间自由切换，各自输出立即呈现
  await tabs.nth(0).click();
  await expect(output).toContainText(originalText.slice(0, 30));
  await tabs.nth(1).click();
  await expect(output).toContainText(originalText.slice(0, 30));
});

test("UE-31-1 两段式裁判：裁判卡展示评论（第一次调用），换裁判重跑的版本同样带评论", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-两段");
  const m1 = await seedModel(request, providerId, "tp-m1");
  const m2 = await seedModel(request, providerId, "tp-m2");
  await seedCouncilPipeline(request, m1, m2, "council-two-phase");

  const result = await makeTrace(request, "council-two-phase");
  expect(result.status_code).toBe(200);

  await page.goto(`/traces/${result.trace_id}`);
  await page.getByRole("tab", { name: /裁判/ }).click();
  const judgeCard = page.locator("section").filter({ has: page.getByLabel("裁判模型") });

  // 原始版本：第 1 轮（评论）折叠块内含输入与评论输出；第 2 轮（最终答案）输出 = 主答案
  const critiqueBlock = judgeCard
    .locator("details")
    .filter({ has: page.getByText("第 1 轮 · 评论", { exact: true }) });
  await critiqueBlock.locator("summary").first().click();
  await critiqueBlock.getByText("输入", { exact: true }).click();
  await expect(critiqueBlock.locator("pre")).toContainText("你将看到用户的问题");
  await critiqueBlock.getByText("输出", { exact: true }).click();
  await expect(critiqueBlock.locator("p")).toContainText("【回答 1】");
  await expect(critiqueBlock.locator("p")).toContainText("可信度");
  await expect(judgeCard.locator("p.text-sm")).toContainText("量子纠缠");

  // 换裁判重跑：切 tab 后点生成，新裁判的两段式输出（评论 + 最终答案）同样完整呈现
  await judgeCard.getByRole("tab").nth(1).click();
  await judgeCard.getByRole("button", { name: "生成", exact: true }).click();
  await expect(page.getByText("裁判聚合（2 个版本）")).toBeVisible({ timeout: 30_000 });
  await expect(critiqueBlock.locator("p")).toContainText("【回答 1】");
  await expect(judgeCard.locator("p.text-sm")).toContainText("量子纠缠");
});
