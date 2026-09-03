import { expect, test } from "@playwright/test";

test("UE-28-1 重置 Key：弹窗展示一次性明文；旧 Key 401、新 Key 200", async ({ page, request }) => {
  // 第一次重置拿 key1（作为"旧 Key"），第二次重置拿 key2（新 Key）
  const first = await request.post("/api/admin/settings/service-key/reset");
  const oldKey = (await first.json()).service_api_key;
  expect(oldKey).toMatch(/^sk-local-/);

  await page.goto("/settings");
  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "重置服务 Key" }).click();

  const modal = page.getByTestId("modal");
  await expect(modal).toBeVisible();
  const newKey = await modal.locator("pre").textContent();
  expect(newKey).toMatch(/^sk-local-/);
  expect(newKey).not.toBe(oldKey);

  // 直接调后端验证：旧 401、新 200（vite 代理转发 /v1）
  const oldResp = await request.get("/v1/models", { headers: { Authorization: `Bearer ${oldKey}` } });
  expect(oldResp.status()).toBe(401);
  const newResp = await request.get("/v1/models", { headers: { Authorization: `Bearer ${newKey}` } });
  expect(newResp.status()).toBe(200);

  await modal.getByRole("button", { name: "我已保存，关闭" }).click();
  await expect(modal).toBeHidden();
});

test("UE-28-2 保留天数：改为 7 后重新查询返回 7", async ({ page, request }) => {
  await page.goto("/settings");
  await page.getByLabel("保留天数").fill("7");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText("已保存 ✓")).toBeVisible();

  const resp = await request.get("/api/admin/settings");
  expect((await resp.json()).log_retention_days).toBe(7);

  // 还原默认，避免影响其他用例
  await request.patch("/api/admin/settings", { data: { log_retention_days: 30 } });
});
