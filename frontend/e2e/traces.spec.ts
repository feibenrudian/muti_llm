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
  await expect(page.getByText("成员调用", { exact: true })).toHaveCount(2);
  await expect(page.getByText("裁判调用")).toBeVisible();
  await expect(page.getByText("最终响应")).toBeVisible();
  // 裁判入参含组装 Prompt（展开后可见成员答案标注）
  const judgeCard = page.locator("section", { hasText: "裁判调用" });
  await judgeCard.getByText("入参（实际发出的完整请求）").click();
  await expect(page.getByText("【回答 1 · deepseek-v4-flash】").first()).toBeVisible();
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
