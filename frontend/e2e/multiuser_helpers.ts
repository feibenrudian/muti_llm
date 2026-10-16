/** 多用户 e2e 辅助（T47/T48）：经管理 API 建用户 / 签发 Key（request fixture 已带会话头）。 */

interface UserOut {
  id: number;
  username: string;
}

interface KeyCreated {
  id: number;
  api_key: string;
  masked: string;
}

export async function createUser(
  request: import("@playwright/test").APIRequestContext,
  username: string,
): Promise<UserOut> {
  const resp = await request.post("/api/admin/users", { data: { username } });
  if (!resp.ok()) throw new Error(`create user failed: ${resp.status()} ${await resp.text()}`);
  return resp.json();
}

export async function issueKey(
  request: import("@playwright/test").APIRequestContext,
  userId: number,
): Promise<KeyCreated> {
  const resp = await request.post(`/api/admin/users/${userId}/keys`, { data: { name: "e2e" } });
  if (!resp.ok()) throw new Error(`issue key failed: ${resp.status()} ${await resp.text()}`);
  return resp.json();
}
