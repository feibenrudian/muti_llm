import { expect, test } from "@playwright/test";

test("UE-29-1 生产模式冒烟：单进程（uvicorn + 静态托管）下全量页面可打开", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("多模型聚合网关")).toBeVisible();
  for (const label of ["供应商", "模型", "组合（Pipeline）", "试运行", "调用日志", "设置"]) {
    await expect(page.getByRole("link", { name: label })).toBeVisible();
  }
  await page.goto("/settings");
  await expect(page.getByText("服务信息")).toBeVisible();
  await page.goto("/traces");
  await expect(page.getByText("调用日志（Trace）")).toBeVisible();
});
