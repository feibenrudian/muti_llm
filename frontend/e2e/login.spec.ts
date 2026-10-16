/** UE-46-1/2：登录页流程（守卫、错误提示、登出、会话失效跳转）。
 *
 * 用 base test（不走 fixtures 的自动登录），才能从"未登录"状态驱动页面。
 */
import { expect, test } from "@playwright/test";

const ADMIN = { username: "admin", password: "e2e-admin-pass" };

test("UE-46-1 登录流程：未登录跳登录页；错误口令提示；正确登录回跳并显示当前用户", async ({
  page,
}) => {
  await page.goto("/providers");
  // 守卫：无令牌访问管理页 → 跳登录
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByTestId("login-form")).toBeVisible();

  // 错误口令 → 统一错误文案
  await page.getByTestId("input-username").fill(ADMIN.username);
  await page.getByTestId("input-password").fill("wrong-password");
  await page.getByTestId("btn-login").click();
  await expect(page.getByRole("alert")).toContainText("用户名或密码错误");

  // 正确口令 → 回跳来源页（/providers），顶栏显示当前用户
  await page.getByTestId("input-password").fill(ADMIN.password);
  await page.getByTestId("btn-login").click();
  await expect(page).toHaveURL(/\/providers/);
  await expect(page.getByTestId("current-user")).toHaveText("管理员");
});

test("UE-46-2 登出与会话失效：登出回登录页；清空会话后访问任意管理页跳登录", async ({
  page,
  request,
}) => {
  // 先走真实登录拿会话（页面内操作）
  await page.goto("/login");
  await page.getByTestId("input-username").fill(ADMIN.username);
  await page.getByTestId("input-password").fill(ADMIN.password);
  await page.getByTestId("btn-login").click();
  await expect(page).toHaveURL(/\/providers/);

  // 登出 → 回登录页；旧会话令牌已在服务端吊销
  await page.getByTestId("btn-logout").click();
  await expect(page).toHaveURL(/\/login/);

  // 服务端校验：未带会话的请求 → 401（request fixture 无默认头）
  const me = await request.get("/api/admin/auth/me");
  expect(me.status()).toBe(401);

  // 无令牌访问其他管理页 → 同样跳登录
  await page.goto("/traces");
  await expect(page).toHaveURL(/\/login/);
});
