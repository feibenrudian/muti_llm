import { promises as fs } from "node:fs";
import { expect, test } from "./fixtures";
import { injectSrs, seedCouncilPipeline, seedModel, seedProvider } from "./helpers";

const QUANTUM = "用一句话解释量子纠缠";

test.beforeEach(async ({ request }) => {
  await request.post("/api/admin/traces/clear");
  await injectSrs(request, { reset: true });
});

test("UE-41-1 看板渲染：跑一次 council 后 KPI/供应商表/模型表出现统计，日期筛选与重置生效", async ({
  page,
  request,
}) => {
  const providerId = await seedProvider(request, "E2E-用量");
  const m1 = await seedModel(request, providerId, "stats-m1");
  const m2 = await seedModel(request, providerId, "stats-m2");
  await seedCouncilPipeline(request, m1, m2, "council-stats");

  const resp = await request.post("/api/admin/playground/run", {
    data: { pipeline_name: "council-stats", message: QUANTUM },
  });
  expect((await resp.json()).status_code).toBe(200);

  await page.goto("/stats");

  // 模型表：两个配置模型各一行（同上游 ID，裁判/成员A 与 成员B）
  const tables = page.locator("table");
  await expect(tables).toHaveCount(4); // 供应商/模型/按用户/按用户×模型（T50）
  const modelRows = tables.nth(1).locator("tbody tr");
  await expect(modelRows).toHaveCount(2);
  await expect(modelRows.first()).toContainText("deepseek-v4-flash");

  // 供应商表合计 tokens = 快照 usage 精确求和（2 成员 88+88 + 评论 259 + 裁判 477 = 912 输入，
  // 输出 117+104+335+287 = 843，合计 1755 → 千分位 1,755）
  const providerRow = tables.nth(0).locator("tbody tr").first();
  await expect(providerRow).toContainText("E2E-用量");
  await expect(providerRow).toContainText("912");
  await expect(providerRow).toContainText("843");
  await expect(providerRow).toContainText("1,755");

  // 日期筛选：起始=明天 → 全部为空；重置 → 数据恢复
  // 本地时区的"明天"（统计端点 date-only 参数按本地日界解释；UTC 日期会在本地跨日时段假失败）
  const tomorrow = new Date(Date.now() + 24 * 3600 * 1000).toLocaleDateString("sv-SE");
  await page.getByLabel("起始日期").fill(tomorrow);
  await expect(page.getByText("所选时间范围内没有调用").first()).toBeVisible();
  await expect(tables.nth(0).locator("tbody tr")).toHaveCount(0);
  await page.getByTestId("reset-filters").click();
  await expect(tables.nth(0).locator("tbody tr")).toHaveCount(1);
});

test("UE-41-2 CSV 导出：供应商表导出触发下载，BOM + 中文表头", async ({ page, request }) => {
  const providerId = await seedProvider(request, "E2E-导出");
  const m1 = await seedModel(request, providerId, "csv-m1");
  const m2 = await seedModel(request, providerId, "csv-m2");
  await seedCouncilPipeline(request, m1, m2, "council-csv");

  const resp = await request.post("/api/admin/playground/run", {
    data: { pipeline_name: "council-csv", message: QUANTUM },
  });
  expect((await resp.json()).status_code).toBe(200);

  await page.goto("/stats");
  const exportButtons = page.getByTestId("export-csv");
  await expect(exportButtons).toHaveCount(2);

  const [download] = await Promise.all([page.waitForEvent("download"), exportButtons.first().click()]);
  expect(download.suggestedFilename()).toBe("usage-by-provider.csv");
  const content = await fs.readFile(await download.path(), "utf8");
  expect(content.charCodeAt(0)).toBe(0xfeff); // BOM：Excel 打开中文不乱码
  expect(content).toContain("供应商,调用数,失败数,输入tokens,缓存命中tokens,缓存写入tokens,未命中tokens,输出tokens,合计tokens");
  expect(content).toContain("E2E-导出");
});

test("UE-50-1 统计页按用户：用户表渲染、筛选联动、空态与恢复", async ({ page, request }) => {
  const username = `ue-stats-${Date.now()}`;
  const user = await (await request.post("/api/admin/users", { data: { username } })).json();
  const key = await (await request.post(`/api/admin/users/${user.id}/keys`, { data: { name: "k" } })).json();
  const provider = await (
    await request.post("/api/admin/providers", {
      data: { name: `stats-p-${Date.now()}`, protocol: "openai_compatible", base_url: "http://127.0.0.1:9801/v1", api_key: "sk-e2e" },
    })
  ).json();
  const model = await (
    await request.post("/api/admin/models", {
      data: { provider_id: provider.id, display_name: "stats-m1", upstream_model_id: "deepseek-v4-flash" },
    })
  ).json();
  const pipeline = await (
    await request.post("/api/admin/pipelines", {
      data: {
        name: `stats-council-${Date.now()}`,
        strategy: "council",
        judge_model_id: model.id,
        members: [{ model_id: model.id, param_overrides: { temperature: 0.7 } }],
      },
    })
  ).json();
  await request.put(`/api/admin/users/${user.id}/grants`, {
    data: { pipelines: [pipeline.id], models: [], providers: [] },
  });
  const resp = await request.post("/v1/chat/completions", {
    data: { model: pipeline.name, messages: [{ role: "user", content: "用一句话解释量子纠缠" }] },
    headers: { Authorization: `Bearer ${key.api_key}` },
  });
  expect(resp.status(), await resp.text()).toBe(200);

  await page.goto("/stats");
  const row = page.getByTestId("stats-user-row").filter({ hasText: username });
  await expect(row).toHaveCount(1);

  // 用户筛选联动：选该用户后按用户×模型表仍有行；起始=明天 → 空态；重置恢复
  await page.getByTestId("stats-user-filter").selectOption(String(user.id));
  await expect(page.getByTestId("stats-user-row")).toHaveCount(1);

  await page.getByLabel("起始日期").fill("2030-01-01");
  await expect(page.getByText("所选时间范围内没有调用").first()).toBeVisible();
  await page.getByTestId("reset-filters").click();
  await expect(page.getByTestId("stats-user-row").filter({ hasText: username })).toHaveCount(1);
});

test("UE-50-3 用户维度 CSV 导出触发下载", async ({ page, request }) => {
  // 造一条调用（Playground 归属 admin）保证"按用户"表非空
  const pipelines = await (await request.get("/api/admin/pipelines")).json();
  if (pipelines.length > 0) {
    await request.post("/api/admin/playground/run", {
      data: { pipeline_name: pipelines[0].name, message: "连通性测试：请只回复 pong" },
    });
  }
  await page.goto("/stats");
  const download = page.waitForEvent("download");
  await page.getByTestId("export-user-csv").click();
  const file = await download;
  expect(file.suggestedFilename()).toBe("usage-by-user.csv");
});

test("UE-50-2 Trace 用户筛选：输入用户名 → 仅该用户记录", async ({ page, request }) => {
  await injectSrs(request, { reset: true }); // 清除其他用例遗留的失败/延迟注入
  const username = `ue-tfilter-${Date.now()}`;
  const user = await (await request.post("/api/admin/users", { data: { username } })).json();
  const key = await (await request.post(`/api/admin/users/${user.id}/keys`, { data: { name: "k" } })).json();
  const provider = await (
    await request.post("/api/admin/providers", {
      data: { name: `tf-p-${Date.now()}`, protocol: "openai_compatible", base_url: "http://127.0.0.1:9801/v1", api_key: "sk-e2e" },
    })
  ).json();
  const model = await (
    await request.post("/api/admin/models", {
      data: { provider_id: provider.id, display_name: "tf-m1", upstream_model_id: "deepseek-v4-flash" },
    })
  ).json();
  // 建 pipeline 并授权 → 用户 Key 调用（council 快照回放，确定性路径）
  const pipeline = await (
    await request.post("/api/admin/pipelines", {
      data: {
        name: `tf-council-${Date.now()}`,
        strategy: "council",
        judge_model_id: model.id,
        members: [{ model_id: model.id, param_overrides: { temperature: 0.7 } }],
      },
    })
  ).json();
  await request.put(`/api/admin/users/${user.id}/grants`, {
    data: { pipelines: [pipeline.id], models: [], providers: [] },
  });
  const resp = await request.post("/v1/chat/completions", {
    data: { model: pipeline.name, messages: [{ role: "user", content: "用一句话解释量子纠缠" }] },
    headers: { Authorization: `Bearer ${key.api_key}` },
  });
  expect(resp.status(), await resp.text()).toBe(200);

  await page.goto("/traces");
  await page.getByTestId("trace-user-filter").fill(username);
  await expect(page.getByText("共 1 条")).toBeVisible();
  await expect(page.getByTestId("trace-user").first()).toHaveText(username);

  // 清空恢复
  await page.getByTestId("trace-user-filter").fill("");
  await expect(page.getByTestId("trace-user").first()).toHaveText(username);
});
