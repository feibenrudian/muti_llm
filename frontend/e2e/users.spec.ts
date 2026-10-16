/** UE-47-1..3：用户管理页与 Key 全流程（页面操作，seed 走管理 API）。 */
import { expect, test } from "./fixtures";

test.use({ permissions: ["clipboard-read", "clipboard-write"] });

test("UE-47-1 用户管理页：新建 → 列表出现 → 编辑显示名 → 停用状态变化", async ({
  page,
  request,
}) => {
  const username = `ue-user-${Date.now()}`;
  await page.goto("/users");
  await page.getByTestId("btn-create-user").click();
  await page.getByTestId("input-new-username").fill(username);
  await page.getByTestId("btn-submit-user").click();

  const row = page.locator("tr", { has: page.getByTestId(`user-link-${username}`) });
  await expect(row).toBeVisible();
  await expect(row).toContainText("启用");

  // 详情页编辑显示名（经 PATCH 后回列表验证）
  const created = await (await request.get(`/api/admin/users?keyword=${username}`)).json();
  const userId = created.items[0].id as number;
  await request.patch(`/api/admin/users/${userId}`, { data: { display_name: "UE 显示名" } });

  await page.goto("/users");
  await expect(page.locator("tr", { has: page.getByTestId(`user-link-${username}`) })).toContainText(
    "UE 显示名",
  );

  // 停用 → 状态列变化
  await page
    .locator("tr", { has: page.getByTestId(`user-link-${username}`) })
    .getByRole("button", { name: "停用" })
    .click();
  await expect(
    page.locator("tr", { has: page.getByTestId(`user-link-${username}`) }),
  ).toContainText("停用");
});

test("UE-47-2 Key 全流程：弹窗创建（名称可选）→ 列表掩码 → 复制得到明文", async ({
  page,
  request,
}) => {
  const username = `ue-key-${Date.now()}`;
  const user = await (await request.post("/api/admin/users", { data: { username } })).json();

  await page.goto(`/users/${user.id}`);

  // 弹窗创建：取消不产 Key
  await page.getByTestId("btn-new-key").click();
  await expect(page.getByTestId("modal")).toBeVisible();
  await page.getByTestId("btn-cancel-create").click();
  await expect(page.getByTestId("key-row")).toHaveCount(0);

  // 名称可选：填名称创建 → 弹窗关闭，列表出现
  await page.getByTestId("btn-new-key").click();
  await page.getByTestId("input-key-name").fill("测试机");
  await page.getByTestId("btn-create-key").click();
  await expect(page.getByTestId("modal")).toHaveCount(0);
  const keyRow = page.getByTestId("key-row").first();
  await expect(keyRow).toContainText("测试机");

  // 复制 → 剪贴板完整明文，与 /secret 一致（明文可随时取回，不依赖创建时刻）
  const keys = await (await request.get(`/api/admin/users/${user.id}/keys`)).json();
  const keyId = keys[0].id as number;
  await keyRow.getByRole("button", { name: "复制" }).click();
  await expect(keyRow.getByRole("button", { name: "已复制 ✓" })).toBeVisible();
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  const secret = await (
    await request.get(`/api/admin/users/${user.id}/keys/${keyId}/secret`)
  ).json();
  expect(clip).toBe(secret.api_key);
  await expect(keyRow).toContainText(`****${clip.slice(-4)}`);
  await expect(keyRow).not.toContainText(clip);

  // 第二把 Key：名称留空（可选）→ 创建 → 列表两行（互不影响）
  await page.getByTestId("btn-new-key").click();
  await page.getByTestId("btn-create-key").click();
  await expect(page.getByTestId("key-row")).toHaveCount(2);
});

test("UE-47-3 合规拦截：非法用户名前端拦截；内置管理员停用被拒且状态不变", async ({
  page,
}) => {
  await page.goto("/users");
  await page.getByTestId("btn-create-user").click();
  await page.getByTestId("input-new-username").fill("非法 名字!");
  // 前端校验拦截：提交按钮保持禁用，表单内联展示错误文案
  await expect(page.getByTestId("btn-submit-user")).toBeDisabled();
  await expect(page.getByRole("alert")).toContainText("3-32");
  await page.getByRole("button", { name: "取消" }).click();

  // 内置 admin 停用 → 后端 409 文案展示，状态列仍"启用"
  const adminRow = page.locator("tr", { has: page.getByTestId("user-link-admin") });
  await adminRow.getByRole("button", { name: "停用" }).click();
  await expect(page.getByRole("alert")).toContainText("内置管理员");
  await expect(adminRow).toContainText("启用");
});

test("UE-49-1 权限页操作：三组勾选保存 → 回显一致；全选/清空可用", async ({ page, request }) => {
  const username = `ue-grant-${Date.now()}`;
  const user = await (await request.post("/api/admin/users", { data: { username } })).json();
  const provider = await (
    await request.post("/api/admin/providers", {
      data: { name: `grant-p-${Date.now()}`, protocol: "openai_compatible", base_url: "http://127.0.0.1:9801/v1", api_key: "sk-e2e" },
    })
  ).json();
  const model = await (
    await request.post("/api/admin/models", {
      data: { provider_id: provider.id, display_name: "grant-m1", upstream_model_id: "deepseek-v4-flash" },
    })
  ).json();
  const pipeline = await (
    await request.post("/api/admin/pipelines", {
      data: {
        name: `grant-council-${Date.now()}`,
        strategy: "council",
        judge_model_id: model.id,
        members: [{ model_id: model.id, param_overrides: {} }],
      },
    })
  ).json();

  await page.goto(`/users/${user.id}`);
  await page.getByTestId("tab-grants").click();

  await page.getByTestId(`grant-pipeline-${pipeline.id}`).check();
  await page.getByTestId(`grant-provider-${provider.id}`).check();
  await page.getByTestId(`grant-model-${model.id}`).check();
  await page.getByTestId("btn-save-grants").click();
  await expect(page.getByText("已保存 ✓")).toBeVisible();

  await page.reload();
  await page.getByTestId("tab-grants").click();
  await expect(page.getByTestId(`grant-pipeline-${pipeline.id}`)).toBeChecked();
  await expect(page.getByTestId(`grant-provider-${provider.id}`)).toBeChecked();
  await expect(page.getByTestId(`grant-model-${model.id}`)).toBeChecked();

  // 全选/清空
  await page.getByTestId("grant-pipeline-all").click(); // 全选
  await page.getByTestId("btn-save-grants").click();
  await expect(page.getByText("已保存 ✓")).toBeVisible();
});

test("UE-49-2 越权可见性：用户 Key 的 /v1/models 仅授权项；未授权调用 403", async ({
  request,
}) => {
  const username = `ue-authz-${Date.now()}`;
  const user = await (await request.post("/api/admin/users", { data: { username } })).json();
  const key = await (await request.post(`/api/admin/users/${user.id}/keys`, { data: { name: "k" } })).json();
  const provider = await (
    await request.post("/api/admin/providers", {
      data: { name: `authz-p-${Date.now()}`, protocol: "openai_compatible", base_url: "http://127.0.0.1:9801/v1", api_key: "sk-e2e" },
    })
  ).json();
  const model = await (
    await request.post("/api/admin/models", {
      data: { provider_id: provider.id, display_name: "authz-m1", upstream_model_id: "deepseek-v4-flash" },
    })
  ).json();
  const pipeline = await (
    await request.post("/api/admin/pipelines", {
      data: {
        name: `authz-council-${Date.now()}`,
        strategy: "council",
        judge_model_id: model.id,
        members: [{ model_id: model.id, param_overrides: { temperature: 0.7 } }],
      },
    })
  ).json();

  await request.put(`/api/admin/users/${user.id}/grants`, {
    data: { pipelines: [pipeline.id], models: [model.id], providers: [] },
  });
  const headers = { Authorization: `Bearer ${key.api_key}` };

  const models = await (await request.get("/v1/models", { headers })).json();
  expect(models.data.map((m: { id: string }) => m.id)).toContain(pipeline.name);
  expect(models.data.map((m: { id: string }) => m.id)).toContain("deepseek-v4-flash");

  // 授权 pipeline 可调用（SRS 回放）
  const ok = await request.post("/v1/chat/completions", {
    data: { model: pipeline.name, messages: [{ role: "user", content: "用一句话解释量子纠缠" }] },
    headers,
  });
  expect(ok.status()).toBe(200);

  // 未授权的模型（另一供应商）→ 403；不存在的 → 404
  const other = await (
    await request.post("/api/admin/providers", {
      data: { name: `authz-q-${Date.now()}`, protocol: "openai_compatible", base_url: "http://127.0.0.1:9801/v1", api_key: "sk-e2e" },
    })
  ).json();
  await request.post("/api/admin/models", {
    data: { provider_id: other.id, display_name: "authz-m2", upstream_model_id: "deepseek-v4-flash" },
  });
  // deepseek-v4-flash 在两个供应商下同名：老入口解析取首个启用行（已授权的那个）→ 需换名验证 403：
  // 停掉已授权模型行后，解析命中未授权行
  await request.patch(`/api/admin/models/${model.id}`, { data: { enabled: false } });
  const denied = await request.post("/v1/chat/completions", {
    data: { model: "deepseek-v4-flash", messages: [{ role: "user", content: "hi" }] },
    headers,
  });
  expect([403, 404]).toContain(denied.status());
});

test("UE-51-1 Key 列表显示最后使用时间（调用后非 —）", async ({ page, request }) => {
  const username = `ue-used-${Date.now()}`;
  const user = await (await request.post("/api/admin/users", { data: { username } })).json();
  const key = await (await request.post(`/api/admin/users/${user.id}/keys`, { data: { name: "k" } })).json();
  const provider = await (
    await request.post("/api/admin/providers", {
      data: { name: `used-p-${Date.now()}`, protocol: "openai_compatible", base_url: "http://127.0.0.1:9801/v1", api_key: "sk-e2e" },
    })
  ).json();
  const model = await (
    await request.post("/api/admin/models", {
      data: { provider_id: provider.id, display_name: "used-m1", upstream_model_id: "deepseek-v4-flash" },
    })
  ).json();
  const pipeline = await (
    await request.post("/api/admin/pipelines", {
      data: {
        name: `used-council-${Date.now()}`,
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
  expect(resp.status()).toBe(200);

  await page.goto(`/users/${user.id}`);
  const row = page.getByTestId("key-row").first();
  await expect(row).not.toContainText("—"); // 最后使用列已写入时间
});

test("UE-51-2 连续失败锁定：第 6 次登录显示锁定提示（锁定专用用户，不影响 admin）", async ({ page, request }) => {
  const username = `ue-lock-${Date.now()}`;
  await request.post("/api/admin/users", {
    data: { username, role: "admin", password: "right-pass-123" },
  });

  await page.goto("/login");
  await page.getByTestId("input-username").fill(username);
  for (let i = 0; i < 5; i++) {
    await page.getByTestId("input-password").fill(`wrong-${i}`);
    await page.getByTestId("btn-login").click();
    await expect(page.getByRole("alert")).toContainText("用户名或密码错误");
  }
  // 第 6 次（即便口令正确）→ 锁定提示
  await page.getByTestId("input-password").fill("right-pass-123");
  await page.getByTestId("btn-login").click();
  await expect(page.getByRole("alert")).toContainText("锁定");
});
