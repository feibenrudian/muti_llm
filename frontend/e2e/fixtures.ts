/** e2e 共享 test 扩展（多用户 T46 测试基建）：
 *
 * - `request` fixture 重绑为携带管理员会话头的上下文：既有 seed/管理 API 调用零改动；
 *   /v1 断言按请求显式传 Authorization 仍然覆盖（extraHTTPHeaders 是默认值）。
 * - `page` 自动登录（addInitScript 注入 sessionStorage 令牌）：既有页面用例零改动。
 *   登录页用例本身改用 base test（@playwright/test），不走自动登录。
 * - SRS 注入（9801）与 /v1 调用不受影响：多带的会话头被各自端点忽略/覆盖。
 */

import {
  test as base,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";

export type { APIRequestContext };

export const ADMIN_USERNAME = "admin";
export const ADMIN_PASSWORD = "e2e-admin-pass";

let cachedToken: string | null = null;

/** 真实调登录端点换取会话令牌（worker 内缓存；workers=1 全局共享）。 */
export async function adminToken(request: APIRequestContext): Promise<string> {
  if (cachedToken) return cachedToken;
  const resp = await request.post("/api/admin/auth/login", {
    data: { username: ADMIN_USERNAME, password: ADMIN_PASSWORD },
  });
  if (!resp.ok()) throw new Error(`admin login failed: ${resp.status()} ${await resp.text()}`);
  cachedToken = ((await resp.json()) as { token: string }).token;
  return cachedToken;
}

/** 页面手动登录辅助（登录页用例断言成功后调用，避免重复输入）。 */
export async function primeSession(page: Page, token: string): Promise<void> {
  await page.addInitScript((t) => sessionStorage.setItem("muti.session", t), token);
}

/** 门户登录（T52）：真实调门户登录端点换令牌（门户用户名独占，不缓存）。 */
export async function portalToken(
  request: APIRequestContext,
  username: string,
  password: string,
): Promise<string> {
  const resp = await request.post("/api/portal/auth/login", {
    data: { username, password },
  });
  if (!resp.ok()) throw new Error(`portal login failed: ${resp.status()} ${await resp.text()}`);
  return ((await resp.json()) as { token: string }).token;
}

/** 门户会话注入（与管理台 muti.session 分 key）。 */
export async function primePortalSession(page: Page, token: string): Promise<void> {
  await page.addInitScript((t) => sessionStorage.setItem("muti.portal.session", t), token);
}

export const test = base.extend({
  request: async ({ playwright, request }, use) => {
    const token = await adminToken(request);
    const authed = await playwright.request.newContext({
      baseURL: test.info().project.use.baseURL,
      extraHTTPHeaders: { authorization: `Bearer ${token}` },
    });
    await use(authed);
    await authed.dispose();
  },
  page: async ({ page, request }, use) => {
    const token = await adminToken(request);
    await page.addInitScript((t) => sessionStorage.setItem("muti.session", t), token);
    await use(page);
  },
});

export { expect };
