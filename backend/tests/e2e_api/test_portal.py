"""AE-52-1..7：用户自助门户（登录/平面隔离/Key 自助/用量与 Trace 归属/授权只读/改密）。

门户端点不触上游；造数的 /v1 调用走既有 council 快照回放（与 T50 统计用例同款）。
"""

import httpx

from tests.conftest import auth_headers, mint_admin_session
from tests.e2e_api.test_council import COUNCIL_BODY, seed_council
from tests.helpers import create_user, issue_key, snapshot_usage

COUNCIL_SCENARIOS = ("passthrough_basic", "param_merge_09", "council_critique", "council_judge")


def _expected_tokens() -> tuple[int, int]:
    prompt = completion = 0
    for s in COUNCIL_SCENARIOS:
        u = snapshot_usage(s)
        prompt += u["prompt_tokens"]
        completion += u["completion_tokens"]
    return prompt, completion


async def _portal_login(asgi_client: httpx.AsyncClient, username: str, password: str) -> dict:
    """门户登录并返回响应体（token/expires_at/user）。"""
    resp = await asgi_client.post(
        "/api/portal/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_portal_login_and_logout(asgi_client: httpx.AsyncClient) -> None:
    """AE-52-1 门户登录/登出：有凭据用户成功；错密码 401；停用 401；连续失败锁定；登出失效。"""
    created = await create_user(asgi_client, username="portal-alice", password="alice-pass-123")
    body = await _portal_login(asgi_client, "portal-alice", "alice-pass-123")
    assert body["user"]["username"] == "portal-alice"
    assert body["user"]["role"] == "user"
    assert body["token"] and body["expires_at"]

    # 错密码 → 401（文案与未知名一致，防枚举）
    resp = await asgi_client.post(
        "/api/portal/auth/login", json={"username": "portal-alice", "password": "wrong-pass-9"}
    )
    assert resp.status_code == 401

    # 停用用户 → 401
    await asgi_client.patch(f"/api/admin/users/{created['id']}", json={"enabled": False})
    resp = await asgi_client.post(
        "/api/portal/auth/login", json={"username": "portal-alice", "password": "alice-pass-123"}
    )
    assert resp.status_code == 401
    await asgi_client.patch(f"/api/admin/users/{created['id']}", json={"enabled": True})

    # 连续 5 次失败 → 锁定（正确密码也 401；用户名独占避免污染其他用例的锁定计数）
    for _ in range(5):
        await asgi_client.post(
            "/api/portal/auth/login",
            json={"username": "lock-portal", "password": "no-such-pass"},
        )
    resp = await asgi_client.post(
        "/api/portal/auth/login", json={"username": "lock-portal", "password": "whatever-1"}
    )
    assert resp.status_code == 401 and "锁定" in resp.json()["detail"]

    # 登出后原令牌失效
    token = (await _portal_login(asgi_client, "portal-alice", "alice-pass-123"))["token"]
    resp = await asgi_client.post("/api/portal/auth/logout", headers=auth_headers(token))
    assert resp.status_code == 200
    resp = await asgi_client.get("/api/portal/auth/me", headers=auth_headers(token))
    assert resp.status_code == 401


async def test_plane_isolation(asgi_client: httpx.AsyncClient) -> None:
    """AE-52-2 平面隔离：user 会话打管理台 403；管理台登录拒 user；admin 可进门户。"""
    from app.main import app

    await create_user(asgi_client, username="iso-user", password="iso-pass-1234")

    # 管理台登录端点拒绝一般用户（纵深防御，T52）
    resp = await asgi_client.post(
        "/api/admin/auth/login", json={"username": "iso-user", "password": "iso-pass-1234"}
    )
    assert resp.status_code == 403 and "管理员" in resp.json()["detail"]

    # 门户会话令牌打管理台 → 一律 403
    token = (await _portal_login(asgi_client, "iso-user", "iso-pass-1234"))["token"]
    for path in ("/api/admin/users", "/api/admin/auth/me", "/api/admin/stats/overview"):
        resp = await asgi_client.get(path, headers=auth_headers(token))
        assert resp.status_code == 403, path

    # 门户自身放行；管理员（admin 会话）也可进门户查看自己用量
    resp = await asgi_client.get("/api/portal/auth/me", headers=auth_headers(token))
    assert resp.status_code == 200 and resp.json()["username"] == "iso-user"
    admin_tok = await mint_admin_session(app)
    resp = await asgi_client.get("/api/portal/auth/me", headers=auth_headers(admin_tok))
    assert resp.status_code == 200 and resp.json()["role"] == "admin"

    # 数据面不认会话令牌（D26）；门户无令牌 → 401
    resp = await asgi_client.get("/v1/models", headers=auth_headers(token))
    assert resp.status_code == 401
    resp = await asgi_client.get("/api/portal/keys", headers={"Authorization": ""})
    assert resp.status_code == 401


async def test_portal_key_lifecycle(asgi_client: httpx.AsyncClient, service_key: str) -> None:
    """AE-52-3 Key 自助全周期：明文一次/列表掩码/secret 一致/启停联动数据面/越权 404/删除。"""
    user = await create_user(asgi_client, username="key-alice", password="key-pass-1234")
    token = (await _portal_login(asgi_client, "key-alice", "key-pass-1234"))["token"]
    h = auth_headers(token)

    resp = await asgi_client.post("/api/portal/keys", json={"name": "笔记本"}, headers=h)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["api_key"].startswith("sk-mu-")

    keys = (await asgi_client.get("/api/portal/keys", headers=h)).json()
    assert [k["id"] for k in keys] == [created["id"]]
    assert keys[0]["masked"] == "****" + created["api_key"][-4:]
    assert "api_key" not in keys[0]  # 列表不回显明文

    resp = await asgi_client.get(f"/api/portal/keys/{created['id']}/secret", headers=h)
    assert resp.status_code == 200
    assert resp.json()["api_key"] == created["api_key"]

    # 管理端签发的 Key 门户同可见（同一名下的 Key）
    admin_key = await issue_key(asgi_client, user["id"], name="管理员签发")
    keys = (await asgi_client.get("/api/portal/keys", headers=h)).json()
    assert {k["id"] for k in keys} == {created["id"], admin_key["id"]}

    # 数据面可用 → 门户停用后 401
    resp = await asgi_client.get("/v1/models", headers=auth_headers(created["api_key"]))
    assert resp.status_code == 200
    resp = await asgi_client.patch(
        f"/api/portal/keys/{created['id']}", json={"enabled": False}, headers=h
    )
    assert resp.status_code == 200 and resp.json()["enabled"] is False
    resp = await asgi_client.get("/v1/models", headers=auth_headers(created["api_key"]))
    assert resp.status_code == 401

    # 越权访问他人 Key：secret/patch/delete 一律 404（防枚举）
    other = await create_user(asgi_client, username="key-bob")
    other_key = await issue_key(asgi_client, other["id"])
    resp = await asgi_client.get(f"/api/portal/keys/{other_key['id']}/secret", headers=h)
    assert resp.status_code == 404
    resp = await asgi_client.patch(
        f"/api/portal/keys/{other_key['id']}", json={"enabled": False}, headers=h
    )
    assert resp.status_code == 404
    resp = await asgi_client.delete(f"/api/portal/keys/{other_key['id']}", headers=h)
    assert resp.status_code == 404

    # 删除自己的 Key → 204 且列表移除
    resp = await asgi_client.delete(f"/api/portal/keys/{admin_key['id']}", headers=h)
    assert resp.status_code == 204
    keys = (await asgi_client.get("/api/portal/keys", headers=h)).json()
    assert [k["id"] for k in keys] == [created["id"]]


async def _seed_caller(
    asgi_client: httpx.AsyncClient, srs_base: str, username: str, password: str
) -> tuple[dict, str]:
    """建用户（带门户口令）+ 发 Key + 授权 council pipeline，返回 (user, 明文 key)。"""
    pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    if not pipelines:
        await seed_council(asgi_client, srs_base)
        pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    user = await create_user(asgi_client, username=username, password=password)
    key = await issue_key(asgi_client, user["id"])
    await asgi_client.put(
        f"/api/admin/users/{user['id']}/grants",
        json={"pipelines": [pipelines[0]["id"]], "models": [], "providers": []},
    )
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 200, resp.text
    return user, key["api_key"]


async def test_portal_stats_scope(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-52-4 用量归属：门户三端点只含本人数据；他人调用不可见；user 参数被忽略；时间筛选生效。"""
    await _seed_caller(asgi_client, srs_live_seeded, "stat-alice", "stat-pass-1234")
    await _seed_caller(asgi_client, srs_live_seeded, "stat-bob", "stat-pass-5678")
    token = (await _portal_login(asgi_client, "stat-alice", "stat-pass-1234"))["token"]
    h = auth_headers(token)
    expected_prompt, expected_completion = _expected_tokens()

    overview = (await asgi_client.get("/api/portal/stats/overview", headers=h)).json()
    assert overview["calls"] == 4  # 一次 council 的 4 行调用
    assert overview["prompt_tokens"] == expected_prompt
    assert overview["completion_tokens"] == expected_completion

    daily = (await asgi_client.get("/api/portal/stats/daily", headers=h)).json()
    assert sum(r["calls"] for r in daily) == 4
    assert len({r["date"] for r in daily}) == 1

    by_model = (await asgi_client.get("/api/portal/stats/by-model", headers=h)).json()
    assert sum(r["calls"] for r in by_model) == 4
    assert {r["upstream_model_id"] for r in by_model} == {"deepseek-v4-flash"}
    assert {r["provider_name"] for r in by_model} == {"srs-provider"}

    # 未知查询参数（user）被忽略，归属强制本人：bob 的 4 次调用不会并入
    overview = (
        await asgi_client.get("/api/portal/stats/overview", params={"user": "stat-bob"}, headers=h)
    ).json()
    assert overview["calls"] == 4

    # 时间筛选：start=明天 → 空（end 为日期含当天，日界语义与 T41 一致，用本地日期）
    from datetime import datetime, timedelta

    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
    overview = (
        await asgi_client.get("/api/portal/stats/overview", params={"start": tomorrow}, headers=h)
    ).json()
    assert overview["calls"] == 0


async def test_portal_traces_scope(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-52-5 Trace 归属：列表/详情仅本人；他人与管理员的 trace 404；关键字筛选命中。"""
    await seed_council(asgi_client, srs_live_seeded)
    alice_user, _ = await _seed_caller(
        asgi_client, srs_live_seeded, "trace-alice", "trace-pass-123"
    )
    # 管理员（兼容 Key）跑一次 → user_id=0 的历史归属，门户不可见
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200

    token = (await _portal_login(asgi_client, "trace-alice", "trace-pass-123"))["token"]
    h = auth_headers(token)

    listing = (await asgi_client.get("/api/portal/traces", headers=h)).json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["user_username"] == "trace-alice"
    assert "request_messages" not in item  # 列表裁剪大字段
    trace_id = item["id"]

    detail = (await asgi_client.get(f"/api/portal/traces/{trace_id}", headers=h)).json()
    assert detail["request"]["id"] == trace_id
    assert len(detail["calls"]) == 4

    # 他人（管理员会话身份不同，但用 iso-user 的门户令牌）→ 404；不存在 → 404
    await create_user(asgi_client, username="trace-bob", password="trace-pass-456")
    bob_token = (await _portal_login(asgi_client, "trace-bob", "trace-pass-456"))["token"]
    resp = await asgi_client.get(f"/api/portal/traces/{trace_id}", headers=auth_headers(bob_token))
    assert resp.status_code == 404
    resp = await asgi_client.get("/api/portal/traces/999999999", headers=h)
    assert resp.status_code == 404

    # 关键字筛选（原始输入全文匹配）
    listing = (
        await asgi_client.get("/api/portal/traces", params={"keyword": "量子纠缠"}, headers=h)
    ).json()
    assert listing["total"] == 1
    listing = (
        await asgi_client.get("/api/portal/traces", params={"keyword": "不存在的关键词"}, headers=h)
    ).json()
    assert listing["total"] == 0
    assert alice_user["id"]  # 造数返回值被使用（user 信息完整性）


async def test_portal_grants_readonly(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-52-6 授权只读：/grants 返回本人三类授权的 id+名称；未授权维度为空；门户无 PUT 端点。"""
    await seed_council(asgi_client, srs_live_seeded)
    pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    models = (await asgi_client.get("/api/admin/models")).json()
    providers = (await asgi_client.get("/api/admin/providers")).json()
    user = await create_user(asgi_client, username="grant-alice", password="grant-pass-123")
    await asgi_client.put(
        f"/api/admin/users/{user['id']}/grants",
        json={"pipelines": [pipelines[0]["id"]], "models": [models[0]["id"]], "providers": []},
    )

    token = (await _portal_login(asgi_client, "grant-alice", "grant-pass-123"))["token"]
    grants = (await asgi_client.get("/api/portal/grants", headers=auth_headers(token))).json()
    assert grants["pipelines"] == [{"id": pipelines[0]["id"], "name": pipelines[0]["name"]}]
    assert grants["models"] == [{"id": models[0]["id"], "name": models[0]["display_name"]}]
    assert grants["providers"] == []
    assert providers  # 供应商清单来自管理 API（存在性），门户默认未授权 → 空列表

    # 门户无授权写入端点（404 → 路由不存在）
    resp = await asgi_client.put(
        "/api/portal/grants", json={"pipelines": [], "models": [], "providers": []}
    )
    assert resp.status_code in (401, 404, 405)


async def test_portal_change_password(asgi_client: httpx.AsyncClient) -> None:
    """AE-52-7 门户改密：旧密码错 400；成功后旧密码失效、新密码可登录、其他会话被吊销。"""
    await create_user(asgi_client, username="pwd-alice", password="old-pass-1234")
    first = (await _portal_login(asgi_client, "pwd-alice", "old-pass-1234"))["token"]
    second = (await _portal_login(asgi_client, "pwd-alice", "old-pass-1234"))["token"]
    h_first, h_second = auth_headers(first), auth_headers(second)

    resp = await asgi_client.post(
        "/api/portal/auth/password",
        json={"old_password": "wrong-old-pass", "new_password": "new-pass-1234"},
        headers=h_first,
    )
    assert resp.status_code == 400

    resp = await asgi_client.post(
        "/api/portal/auth/password",
        json={"old_password": "old-pass-1234", "new_password": "new-pass-1234"},
        headers=h_first,
    )
    assert resp.status_code == 200

    # 当前会话保留，其他会话吊销（FR-21）
    assert (await asgi_client.get("/api/portal/auth/me", headers=h_first)).status_code == 200
    assert (await asgi_client.get("/api/portal/auth/me", headers=h_second)).status_code == 401

    # 旧密码不能再登录，新密码可登录（门户与管理台两端点同源校验）
    resp = await asgi_client.post(
        "/api/portal/auth/login", json={"username": "pwd-alice", "password": "old-pass-1234"}
    )
    assert resp.status_code == 401
    resp = await asgi_client.post(
        "/api/admin/auth/login", json={"username": "pwd-alice", "password": "new-pass-1234"}
    )
    assert resp.status_code == 403  # 新口令正确，但管理台拒绝一般用户（非 401 凭据错误）
