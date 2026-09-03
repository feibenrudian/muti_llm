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

test("UE-27-2 详情时间线：原始请求 → 成员×2 → 裁判 → 最终答案", async ({ page, request }) => {
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
  await expect(page.getByText(/成员调用 \d/)).toHaveCount(2);
  await expect(page.getByText("裁判调用")).toBeVisible();
  await expect(page.getByText(/Ⓝ 最终响应/)).toBeVisible();
  // 快速定位：模型多时点锚点直达目标成员卡片，无需长页滚动
  await page.getByRole("button", { name: /成员2 · / }).click();
  await expect(page.getByText("成员调用 2")).toBeInViewport();
  // 成员调用的入参与输出均默认折叠，模型多时保持页面紧凑，按需展开
  const outputDetails = page.locator('details:has(summary:text-is("输出"))');
  await expect(outputDetails).toHaveCount(2); // 裁判输出改为直出展示，折叠块仅成员
  await expect(outputDetails.first()).not.toHaveAttribute("open");
  // 裁判入参含组装 Prompt（展开后可见成员答案标注）；输出直接可见且即最终答案
  const judgeCard = page.locator("section", { hasText: "裁判调用" });
  await judgeCard.getByText("入参（实际发出的完整请求）").click();
  await expect(page.getByText("【回答 1 · deepseek-v4-flash】").first()).toBeVisible();
  const finalText = await page
    .locator("section", { hasText: "最终响应" })
    .locator("p")
    .innerText();
  await expect(judgeCard.locator("p").last()).toHaveText(finalText);
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
  await expect(page.getByText(/injected failure|上游/).first()).toBeVisible();
  await expect(page.getByText("裁判调用")).toHaveCount(0);
});

test("UE-30-1 换裁判重跑：下拉选模型即时流式输出，可在版本间切换", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-重跑");
  const m1 = await seedModel(request, providerId, "rj-m1");
  const m2 = await seedModel(request, providerId, "rj-m2");
  await seedCouncilPipeline(request, m1, m2, "council-rejudge");

  const result = await makeTrace(request, "council-rejudge");
  expect(result.status_code).toBe(200);

  await page.goto(`/traces/${result.trace_id}`);
  const judgeCard = page.locator("section").filter({ has: page.getByLabel("裁判模型") });
  const modelSelect = judgeCard.getByLabel("裁判模型");
  const output = judgeCard.locator("p.whitespace-pre-wrap");
  const originalText = (await output.innerText()).replace(/\s+/g, " ").trim();

  // 下拉默认当前裁判模型；选项仅限本次请求的成员 + 裁判模型
  await expect(modelSelect).toHaveValue(String(m1));
  await expect(modelSelect.locator("option")).toHaveCount(2);

  // 首次换模型（m2 是成员但从未当过裁判）：注入失败 → 流式重跑失败，输出区显示错误
  await injectSrs(request, { fail_times: { "deepseek-v4-flash": 20 } });
  await modelSelect.selectOption(String(m2));
  await expect(judgeCard.getByText(/injected failure|上游|超时/)).toBeVisible();

  // 切回原始裁判：立即显示原始答案（不重新调用）
  await injectSrs(request, { reset: true });
  await modelSelect.selectOption(String(m1));
  await expect(output).toContainText(originalText.slice(0, 30));

  // 再次选择 m2：失败尝试不算已生成结果 → 重新发起并成功，输出实时更新
  await modelSelect.selectOption(String(m2));
  await expect(page.getByText("裁判聚合（2 个版本）")).toBeVisible();
  await expect(output).toContainText(originalText.slice(0, 30)); // 同一快照回放，内容与原始答案一致
  await expect(modelSelect).toHaveValue(String(m2));

  // 在两个版本间自由切换，各自输出立即呈现
  await modelSelect.selectOption(String(m1));
  await expect(output).toContainText(originalText.slice(0, 30));
  await modelSelect.selectOption(String(m2));
  await expect(output).toContainText(originalText.slice(0, 30));
});
