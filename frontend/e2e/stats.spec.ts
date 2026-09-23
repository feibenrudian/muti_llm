import { promises as fs } from "node:fs";
import { expect, test } from "@playwright/test";
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
  await expect(tables).toHaveCount(2);
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
  const tomorrow = new Date(Date.now() + 24 * 3600 * 1000).toISOString().slice(0, 10);
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
