"""AE-48-1..7：数据面身份与归属（调用 / 落库 / 兼容 Key / Playground / rejudge / 零漂移）。"""

import httpx

from app.orm import ModelCallLog, RequestLog
from app.repos import Repository, list_model_calls
from tests.conftest import auth_headers
from tests.e2e_api.test_council import COUNCIL_BODY, seed_council
from tests.helpers import create_user, issue_key, snapshot_content, snapshot_usage

QUANTUM = "用一句话解释量子纠缠"


async def _grant_everything(client: httpx.AsyncClient, user_id: int) -> None:
    """T49 后数据面默认拒绝：测试用户授予现存全部 pipeline/model/provider。"""
    pipelines = (await client.get("/api/admin/pipelines")).json()
    models = (await client.get("/api/admin/models")).json()
    providers = (await client.get("/api/admin/providers")).json()
    resp = await client.put(
        f"/api/admin/users/{user_id}/grants",
        json={
            "pipelines": [p["id"] for p in pipelines],
            "models": [m["id"] for m in models],
            "providers": [p["id"] for p in providers],
        },
    )
    assert resp.status_code == 200


# 与快照 model_test_msg 一致的透传载荷（temp 0.0 + 固定测试消息，逐字节命中回放）
PASSTHROUGH_BODY = {
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "连通性测试：请只回复 pong"}],
    "temperature": 0.0,
}


async def latest_trace(app: object) -> tuple[RequestLog, list[ModelCallLog]]:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        requests = await Repository(session, RequestLog).list()
        trace = requests[-1]
        return trace, await list_model_calls(session, trace.id)


async def test_user_key_calls_council(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-48-1 用户 Key 可调用：content/usage 与快照一致；/v1/models 与管理 Key 一致。"""
    seeded = await seed_council(asgi_client, srs_live_seeded)
    alice = await create_user(asgi_client, username="alice")
    key = await issue_key(asgi_client, alice["id"])
    await _grant_everything(asgi_client, alice["id"])

    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == snapshot_content("council_judge")
    expected_prompt = 0
    expected_completion = 0
    for scenario in ("passthrough_basic", "param_merge_09", "council_critique", "council_judge"):
        usage = snapshot_usage(scenario)
        expected_prompt += usage["prompt_tokens"]
        expected_completion += usage["completion_tokens"]
    assert body["usage"]["prompt_tokens"] == expected_prompt
    assert body["usage"]["completion_tokens"] == expected_completion
    assert seeded  # seed 使用

    # 本任务尚无权限过滤：用户 Key 与服务 Key 的 /v1/models 内容一致
    user_models = (await asgi_client.get("/v1/models", headers=auth_headers(key["api_key"]))).json()
    admin_models = (await asgi_client.get("/v1/models", headers=auth_headers(service_key))).json()
    assert user_models == admin_models


async def test_attribution_persisted(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-48-2 归属落库：透传与 council 两条路径，request_logs 与全部 model_call_logs 同归属。"""
    from app.main import app

    await seed_council(asgi_client, srs_live_seeded)
    alice = await create_user(asgi_client, username="alice")
    key = await issue_key(asgi_client, alice["id"])
    await _grant_everything(asgi_client, alice["id"])

    # 透传（非流式）
    resp = await asgi_client.post(
        "/v1/chat/completions", json=PASSTHROUGH_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 200
    trace, calls = await latest_trace(app)
    assert trace.user_id == alice["id"] and trace.user_username == "alice"
    assert all(c.user_id == alice["id"] for c in calls)

    # council 非流式
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 200
    trace, calls = await latest_trace(app)
    assert trace.user_id == alice["id"] and trace.user_username == "alice"
    assert len(calls) == 4  # 2 成员 + 评论 + 终局
    assert {c.user_id for c in calls} == {alice["id"]}


async def test_legacy_key_attribution_and_zero_regression(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-48-3 兼容 Key：归属 admin；响应与升级前逐字段一致。"""
    from app.main import app

    await seed_council(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "council-v1"
    assert set(body.keys()) == {"id", "object", "created", "model", "choices", "usage"}

    trace, calls = await latest_trace(app)
    assert trace.user_username == "admin" and trace.user_id == app.state.admin_user_id
    assert {c.user_username for c in calls} == {"admin"}


async def test_identity_error_semantics(
    asgi_client: httpx.AsyncClient, srs_live_seeded: str
) -> None:
    """AE-48-4 错误语义：停用 Key → 401；停用用户 → 403；Key 打管理 API → 401。"""
    await seed_council(asgi_client, srs_live_seeded)
    alice = await create_user(asgi_client, username="alice")
    key = await issue_key(asgi_client, alice["id"])
    key_id = key["id"]

    resp = await asgi_client.patch(
        f"/api/admin/users/{alice['id']}/keys/{key_id}", json={"enabled": False}
    )
    assert resp.status_code == 200
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"

    # 重新启用 Key、停用用户 → 403 user_disabled
    await asgi_client.patch(f"/api/admin/users/{alice['id']}/keys/{key_id}", json={"enabled": True})
    await asgi_client.patch(f"/api/admin/users/{alice['id']}", json={"enabled": False})
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "user_disabled"

    # 数据面 Key 不被管理面接受
    await asgi_client.patch(f"/api/admin/users/{alice['id']}", json={"enabled": True})
    resp = await asgi_client.get("/api/admin/users", headers=auth_headers(key["api_key"]))
    assert resp.status_code == 401


async def test_playground_attribution(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-48-5 Playground 归属发起管理员（via=playground）。"""
    from app.main import app

    await seed_council(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/api/admin/playground/run",
        json={"pipeline_name": "council-v1", "message": QUANTUM},
    )
    assert resp.status_code == 200
    trace, calls = await latest_trace(app)
    assert trace.user_username == "admin"
    assert {c.user_username for c in calls} == {"admin"}


async def test_rejudge_attribution_inherited(
    asgi_client: httpx.AsyncClient, srs_live_seeded: str
) -> None:
    """AE-48-6 换裁判重跑：judge_critique/judge_rerun 行继承原 trace 归属。"""
    from app.main import app

    await seed_council(asgi_client, srs_live_seeded)
    alice = await create_user(asgi_client, username="alice")
    key = await issue_key(asgi_client, alice["id"])
    await _grant_everything(asgi_client, alice["id"])
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 200
    trace, _ = await latest_trace(app)

    models = (await asgi_client.get("/api/admin/models")).json()
    member_model_id = models[0]["id"]
    resp = await asgi_client.post(
        f"/api/admin/traces/{trace.id}/rejudge", json={"judge_model_id": member_model_id}
    )
    assert resp.status_code == 200, resp.text

    _, calls = await latest_trace(app)
    rerun = [c for c in calls if c.role in ("judge_critique", "judge_rerun")]
    assert len(rerun) >= 2
    assert {c.user_id for c in rerun} == {alice["id"]}


async def test_upstream_payload_zero_drift(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-48-7 载荷零漂移：同一场景分别用用户 Key 与兼容 Key → SRS 录像的上游请求逐字节相同。"""
    await seed_council(asgi_client, srs_live_seeded)
    alice = await create_user(asgi_client, username="alice")
    key = await issue_key(asgi_client, alice["id"])
    await _grant_everything(asgi_client, alice["id"])

    async with httpx.AsyncClient() as srs:
        await srs.post(f"{srs_live_seeded}/_test/config", json={"reset": True})
    resp1 = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp1.status_code == 200
    async with httpx.AsyncClient() as srs:
        recorded_user = (await srs.get(f"{srs_live_seeded}/_test/requests")).json()

        await srs.post(f"{srs_live_seeded}/_test/config", json={"reset": True})
    resp2 = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )
    assert resp2.status_code == 200
    async with httpx.AsyncClient() as srs:
        recorded_legacy = (await srs.get(f"{srs_live_seeded}/_test/requests")).json()

    # body 逐字节相同（录像的 ts 时间戳与身份无关，不比较）
    assert [r["body"] for r in recorded_user["requests"]] == [
        r["body"] for r in recorded_legacy["requests"]
    ]
