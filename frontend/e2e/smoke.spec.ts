import { expect, test } from "@playwright/test";

test("UE-20-1 冒烟：应用可打开，导航 6 项，无控制台报错", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });

  await page.goto("/");
  await expect(page.getByText("多模型聚合网关")).toBeVisible();
  for (const label of ["供应商", "模型", "组合（Pipeline）", "试运行", "调用日志", "设置"]) {
    await expect(page.getByRole("link", { name: label })).toBeVisible();
  }
  expect(errors).toEqual([]);
});
