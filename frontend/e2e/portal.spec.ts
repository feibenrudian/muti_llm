/** UE-52-1..5：用户自助门户（登录 / 用量 / Key 自助 / 调用记录 / 平面隔离）。 */

import type { APIRequestContext } from "@playwright/test";
import { expect, primePortalSession, portalToken, test } from "./fixtures";
import { injectSrs, seedModel, seedProvider } from "./helpers";

test.use({ permissions: ["clipboard-read", "clipboard-write"] });

const PASSWORD = "portal-pass-123";
const QUESTION = "用一句话解释量子纠缠";

/** 造一个可调用 council 的门户用户：用户（带口令）+ Key + provider/model/pipeline + 授权。 */
async function seedCaller(
  request: APIRequestContext,
  prefix: string,
  modelName: string,
): Promise<{ user: { id: number; username: string }; key: { api_key: string }; pipeline: { id: number; name: string }; upstream: string }> {
  const username = `${prefix}-${Date.now()}`;
  const user = await (
    await request.post("/api/admin/users", {
      data: { username, password: PASSWORD, display_name: username },
    })
  ).json();
  const key = await (
    await request.post(`/api/admin/users/${user.id}/keys`, { data: { name: "k" } })
  ).json();
  const providerId = await seedProvider(request, `${prefix}-p-${Date.now()}`);
  const modelId = await seedModel(request, providerId, modelName);
  const pipeline = await (
    await request.post("/api/admin/pipelines", {
      data: {
        name: `${prefix}-pipe-${Date.now()}`,
        strategy: "council",
        judge_model_id: modelId,
        members: [{ model_id: modelId, param_overrides: { temperature: 0.7 } }],
      },
    })
  ).json();
  await request.put(`/api/admin/users/${user.id}/grants`, {
    data: { pipelines: [pipeline.id], models: [], providers: [] },
  });
  return { user, key, pipeline, upstream: "deepseek-v4-flash" };
}

async function callAsUser(
  request: APIRequestContext,
  apiKey: string,
  pipelineName: string,
): Promise<void> {
  const resp = await request.post("/v1/chat/completions", {
    data: { model: pipelineName, messages: [{ role: "user", content: QUESTION }] },
    headers: { Authorization: `Bearer ${apiKey}` },
  });
  expect(resp.status(), await resp.text()).toBe(200);
}

test.beforeEach(async ({ request }) => {
  await request.post("/api/admin/traces/clear");
  await injectSrs(request, { reset: true });
});

test("UE-52-1 门户登录流程：错密码就地报错；成功进门户；登出回登录页", async ({ page }) => {
  await page.goto("/portal/login");
  await page.getByTestId("portal-input-username").fill("no-such-user");
  await page.getByTestId("portal-input-password").fill("wrong-pass-123");
  await page.getByTestId("portal-btn-login").click();
  await expect(page.getByRole("alert")).toContainText("用户名或密码错误");

  await page.getByTestId("portal-input-username").fill("admin");
  await page.getByTestId("portal-input-password").fill("e2e-admin-pass");
  await page.getByTestId("portal-btn-login").click();
  await expect(page).toHaveURL(/\/portal$/);
  // 顶栏显示 display_name（内置 admin 为"管理员"）或用户名；"…" 表示 me 查询未完成
  await expect(page.getByTestId("portal-current-user")).toHaveText(/^(管理员|admin)$/);

  await page.getByTestId("portal-btn-logout").click();
  await expect(page).toHaveURL(/\/portal\/login$/);
});

test("UE-52-2 门户用量页：只显示本人调用的模型与统计；时间筛选与重置生效", async ({
  page,
  request,
}) => {
  const alice = await seedCaller(request, "ue-portal-a", "portal-m1");
  await callAsUser(request, alice.key.api_key, alice.pipeline.name);
  // 第二个用户的调用（不同展示名）：不应出现在 alice 的门户视图
  const bob = await seedCaller(request, "ue-portal-b", "portal-m2");
  await callAsUser(request, bob.key.api_key, bob.pipeline.name);

  const token = await portalToken(request, alice.user.username, PASSWORD);
  await primePortalSession(page, token);
  await page.goto("/portal");

  // 按模型表：只有 alice 自己的一行（bob 的 portal-m2 展示名不可见）
  const table = page.getByTestId("portal-usage-by-model");
  await expect(table).toContainText("共 1 行");
  await expect(table).toContainText(alice.upstream);
  await expect(table).not.toContainText("portal-m2");

  // 时间筛选：起始=明天 → 空态；重置 → 恢复
  await page.getByTestId("portal-stats-start").fill("2030-01-01");
  await expect(page.getByText("所选时间范围内没有调用").first()).toBeVisible();
  await page.getByTestId("portal-stats-reset").click();
  await expect(table).toContainText("共 1 行");
});

test("UE-52-3 门户 Key 管理：弹窗创建（名称可选）→ 列表复制取明文 → 停用 → 删除；可调用范围只读", async ({
  page,
  request,
}) => {
  const alice = await seedCaller(request, "ue-portal-key", "portal-key-m1");
  const token = await portalToken(request, alice.user.username, PASSWORD);
  await primePortalSession(page, token);
  await page.goto("/portal/keys");

  // 管理端签发的既有 Key 在列表
  await expect(page.getByTestId("portal-key-row").first()).toContainText("k");

  // 弹窗创建：取消不产 Key
  await page.getByTestId("portal-btn-new-key").click();
  await expect(page.getByTestId("modal")).toBeVisible();
  await page.getByTestId("portal-btn-cancel-create").click();
  await expect(page.getByTestId("portal-key-row")).toHaveCount(1);

  // 名称可选：填"笔记本"创建 → 弹窗关闭，列表出现
  await page.getByTestId("portal-btn-new-key").click();
  await page.getByTestId("portal-input-key-name").fill("笔记本");
  await page.getByTestId("portal-btn-create-key").click();
  const newRow = page.getByTestId("portal-key-row").filter({ hasText: "笔记本" });
  await expect(newRow).toHaveCount(1);

  // 列表"复制"随时取回明文（与 /secret 一致，不依赖创建时刻）
  const keys = await (await request.get(`/api/admin/users/${alice.user.id}/keys`)).json();
  const created = keys.find((k: { name: string }) => k.name === "笔记本");
  await newRow.getByTestId(`portal-btn-copy-key-${created.id}`).click();
  await expect(newRow.getByTestId(`portal-btn-copy-key-${created.id}`)).toContainText("已复制");
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  const secret = await (
    await request.get(`/api/admin/users/${alice.user.id}/keys/${created.id}/secret`)
  ).json();
  expect(clip).toBe(secret.api_key);

  // 停用 → 徽标变化；再启用
  await newRow.getByRole("button", { name: "停用" }).click();
  await expect(newRow).toContainText("停用");
  await newRow.getByRole("button", { name: "启用" }).click();
  await expect(newRow).toContainText("启用");

  // 删除（confirm 对话框）
  page.once("dialog", (dialog) => void dialog.accept());
  await newRow.getByRole("button", { name: "删除" }).click();
  await expect(page.getByTestId("portal-key-row").filter({ hasText: "笔记本" })).toHaveCount(0);

  // 可调用范围（只读授权）：显示被授权的 pipeline 名，未授权维度显示空态
  await expect(page.getByTestId("portal-grant-pipelines")).toContainText(alice.pipeline.name);
  await expect(page.getByTestId("portal-grant-models")).toContainText("（未授权）");
});

test("UE-52-4 门户调用日志：列表只含本人记录；详情只读；他人 trace 打不开", async ({
  page,
  request,
  browser,
}) => {
  const alice = await seedCaller(request, "ue-portal-t", "portal-t-m1");
  await callAsUser(request, alice.key.api_key, alice.pipeline.name);

  const token = await portalToken(request, alice.user.username, PASSWORD);
  await primePortalSession(page, token);
  await page.goto("/portal/traces");
  await expect(page.getByText("共 1 条")).toBeVisible();

  // 详情（只读）：原始请求 / 最终响应 / 裁判调用可见
  await page.getByRole("link", { name: "详情" }).click();
  await expect(page.getByRole("heading", { name: /调用记录 #\d+/ })).toBeVisible();
  await expect(page.getByText("原始请求")).toBeVisible();
  await expect(page.getByText("最终响应")).toBeVisible();
  await expect(page.getByText(/裁判调用/)).toBeVisible();
  const traceId = page.url().split("/").pop()!;

  // 他人（bob）打开 alice 的 trace → 404 → 页面显示加载失败
  const bob = await seedCaller(request, "ue-portal-tb", "portal-tb-m1");
  const bobToken = await portalToken(request, bob.user.username, PASSWORD);
  const bobContext = await browser.newContext();
  const bobPage = await bobContext.newPage();
  await primePortalSession(bobPage, bobToken);
  await bobPage.goto(`/portal/traces/${traceId}`);
  await expect(bobPage.getByText(/加载失败/)).toBeVisible();
  await bobContext.close();
});

test("UE-52-5 平面隔离：仅持门户会话访问管理台路由 → 跳管理台登录页", async ({
  request,
  browser,
}) => {
  const alice = await seedCaller(request, "ue-portal-iso", "portal-iso-m1");
  const token = await portalToken(request, alice.user.username, PASSWORD);
  // 全新 context（fixtures 的 page 会自动注入管理台会话，这里必须只用门户会话）
  const context = await browser.newContext();
  const page = await context.newPage();
  await primePortalSession(page, token);
  await page.goto("/users");
  await expect(page).toHaveURL(/\/login$/);
  await context.close();
});

test("UE-52-6 登录入口互链：普通用户在管理台登录被引导去门户并预填用户名", async ({
  page,
  request,
}) => {
  const alice = await seedCaller(request, "ue-portal-guide", "portal-guide-m1");

  // 两个登录页互链常驻（此时均无门户会话，登录表单可见）
  await page.goto("/login");
  await expect(page.getByTestId("link-portal-login")).toBeVisible();
  await page.goto("/portal/login");
  await expect(page.getByTestId("link-admin-login")).toBeVisible();

  // 普通用户在管理台登录：密码正确但角色不符 → 403 引导按钮
  await page.goto("/login");
  await page.getByTestId("input-username").fill(alice.user.username);
  await page.getByTestId("input-password").fill(PASSWORD);
  await page.getByTestId("btn-login").click();
  await expect(page.getByRole("alert")).toContainText("门户");
  await page.getByTestId("btn-go-portal").click();

  // 到门户登录页：用户名已预填，输密码即可进入门户
  await expect(page).toHaveURL(/\/portal\/login$/);
  await expect(page.getByTestId("portal-input-username")).toHaveValue(alice.user.username);
  await page.getByTestId("portal-input-password").fill(PASSWORD);
  await page.getByTestId("portal-btn-login").click();
  await expect(page).toHaveURL(/\/portal$/);
  await expect(page.getByTestId("portal-current-user")).not.toHaveText("…");
});

test("UE-52-7 默认入口：未登录进站直达门户登录页；门户登录后再进站直达门户首页", async ({
  request,
  browser,
}) => {
  // 全新 context（无任何会话）：进站 → 用户门户登录页（而非管理台登录页）
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await expect(page).toHaveURL(/\/portal\/login$/);
  await expect(page.getByTestId("portal-login-form")).toBeVisible();

  // 门户登录后再进站 → 直达门户首页（不再显示登录表单）
  const alice = await seedCaller(request, "ue-portal-root", "portal-root-m1");
  const token = await portalToken(request, alice.user.username, PASSWORD);
  await primePortalSession(page, token);
  await page.goto("/");
  await expect(page).toHaveURL(/\/portal$/);
  await expect(page.getByTestId("portal-current-user")).not.toHaveText("…");
  await context.close();
});
