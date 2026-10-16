"""AE-50-1..4：按用户统计与 Trace 用户筛选（聚合正确性 / 历史行 / 组合筛选）。"""

import httpx

from tests.conftest import auth_headers
from tests.e2e_api.test_council import COUNCIL_BODY, seed_council
from tests.helpers import create_user, issue_key, snapshot_usage

QUANTUM = "用一句话解释量子纠缠"


async def _run_for_user(
    asgi_client: httpx.AsyncClient, srs_base: str, username: str, times: int = 1
) -> dict:
    """建用户发 Key 授权并跑 N 次 council（快照回放）；council 已存在则复用（provider 名唯一）。"""
    pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    if not pipelines:
        await seed_council(asgi_client, srs_base)
        pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    user = await create_user(asgi_client, username=username)
    key = await issue_key(asgi_client, user["id"])
    await asgi_client.put(
        f"/api/admin/users/{user['id']}/grants",
        json={"pipelines": [pipelines[0]["id"]], "models": [], "providers": []},
    )
    for _ in range(times):
        resp = await asgi_client.post(
            "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(key["api_key"])
        )
        assert resp.status_code == 200
    return user


def _expected_tokens(scenarios: tuple[str, ...]) -> tuple[int, int]:
    prompt = completion = 0
    for s in scenarios:
        u = snapshot_usage(s)
        prompt += u["prompt_tokens"]
        completion += u["completion_tokens"]
    return prompt, completion


async def test_user_filter_matches_db(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-50-1 user 筛选：traces 与 stats 四端点带 user = 该用户调用行求和；用户名/数字 id 等价。"""
    from app.main import app

    await seed_council(asgi_client, srs_live_seeded)
    alice = await create_user(asgi_client, username="alice-stat")
    key = await issue_key(asgi_client, alice["id"])
    pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    await asgi_client.put(
        f"/api/admin/users/{alice['id']}/grants",
        json={"pipelines": [pipelines[0]["id"]], "models": [], "providers": []},
    )
    # alice 一次 + 兼容 Key（admin）一次
    for headers in (auth_headers(key["api_key"]), auth_headers(service_key)):
        await asgi_client.post("/v1/chat/completions", json=COUNCIL_BODY, headers=headers)

    expected_prompt, expected_completion = _expected_tokens(
        ("passthrough_basic", "param_merge_09", "council_critique", "council_judge")
    )

    async with app.state.session_factory() as session:
        from sqlalchemy import func, select

        from app.orm import ModelCallLog

        row = (
            await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(ModelCallLog.prompt_tokens), 0),
                    func.coalesce(func.sum(ModelCallLog.completion_tokens), 0),
                ).where(ModelCallLog.user_id == alice["id"])
            )
        ).one()
    db_calls, db_prompt, db_completion = int(row[0]), int(row[1]), int(row[2])
    assert db_calls == 4  # 一次 council 的 4 行调用

    for uid in (str(alice["id"]), "alice-stat"):
        overview = (await asgi_client.get("/api/admin/stats/overview", params={"user": uid})).json()
        assert overview["calls"] == db_calls
        assert overview["prompt_tokens"] == db_prompt == expected_prompt
        assert overview["completion_tokens"] == db_completion == expected_completion

    # traces 筛选：仅 alice 的 1 条
    traces = (await asgi_client.get("/api/admin/traces", params={"user": "alice-stat"})).json()
    assert traces["total"] == 1
    assert traces["items"][0]["user_username"] == "alice-stat"
    traces = (await asgi_client.get("/api/admin/traces", params={"user": str(alice["id"])})).json()
    assert traces["total"] == 1


async def test_by_user_aggregation(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-50-2 by-user 聚合：多用户各行与库内求和一致；历史行（user_id=0）单列。"""
    await _run_for_user(asgi_client, srs_live_seeded, "bob-stat")
    await _run_for_user(asgi_client, srs_live_seeded, "carol-stat")
    # admin（兼容 Key）跑一次 → user_username=admin（council 已由 _run_for_user 播种）
    await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )

    rows = (await asgi_client.get("/api/admin/stats/by-user")).json()
    by_name = {r["user_username"]: r for r in rows}
    assert set(by_name) >= {"bob-stat", "carol-stat", "admin"}

    expected_prompt, expected_completion = _expected_tokens(
        ("passthrough_basic", "param_merge_09", "council_critique", "council_judge")
    )
    assert by_name["bob-stat"]["calls"] == 4
    assert by_name["bob-stat"]["prompt_tokens"] == expected_prompt
    assert by_name["carol-stat"]["completion_tokens"] == expected_completion

    # by-provider / by-model / daily 带 user 筛选联动
    provider_rows = (
        await asgi_client.get("/api/admin/stats/by-provider", params={"user": "bob-stat"})
    ).json()
    assert sum(r["calls"] for r in provider_rows) == 4


async def test_by_user_model_aggregation(
    asgi_client: httpx.AsyncClient, srs_live_seeded: str
) -> None:
    """AE-50-3 by-user-model：用户×供应商×模型分组正确；删除模型后行保留（冗余字段）。"""
    await _run_for_user(asgi_client, srs_live_seeded, "dave-stat")

    rows = (await asgi_client.get("/api/admin/stats/by-user-model")).json()
    dave = [r for r in rows if r["user_username"] == "dave-stat"]
    assert len(dave) >= 1
    assert {r["provider_name"] for r in dave} == {"srs-provider"}
    assert {r["upstream_model_id"] for r in dave} == {"deepseek-v4-flash"}

    # 删除 dave 用过的模型 → 其行保留（display_name 联表为 null）；未删除的另一行照常联表
    deleted_id = dave[0]["model_id"]
    assert (await asgi_client.delete(f"/api/admin/models/{deleted_id}")).status_code == 204
    rows = (await asgi_client.get("/api/admin/stats/by-user-model")).json()
    dave = [r for r in rows if r["user_username"] == "dave-stat"]
    assert dave
    names = {(r["model_id"], r["display_name"]) for r in dave}
    assert (deleted_id, None) in names  # 已删模型的行保留


async def test_combined_filters_and_time_semantics(
    asgi_client: httpx.AsyncClient, srs_live_seeded: str
) -> None:
    """AE-50-4 组合筛选：user+provider+日期范围组合命中；start=明天 → 空；end 含当天。"""
    await _run_for_user(asgi_client, srs_live_seeded, "erin-stat")

    overview = (
        await asgi_client.get(
            "/api/admin/stats/overview",
            params={"user": "erin-stat", "provider": "srs-provider"},
        )
    ).json()
    assert overview["calls"] == 4

    overview = (
        await asgi_client.get(
            "/api/admin/stats/overview",
            params={"user": "erin-stat", "provider": "no-such-provider"},
        )
    ).json()
    assert overview["calls"] == 0

    # end 为今天（含当天）→ 命中；start 为明天 → 空
    # 日界语义与 T41 一致：date-only 参数按本地时区解释（astimezone），故此处用本地日期
    from datetime import datetime, timedelta

    today = datetime.now().astimezone().date().isoformat()
    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
    overview = (
        await asgi_client.get(
            "/api/admin/stats/overview",
            params={"user": "erin-stat", "start": today, "end": today},
        )
    ).json()
    assert overview["calls"] == 4
    overview = (
        await asgi_client.get(
            "/api/admin/stats/overview",
            params={"user": "erin-stat", "start": tomorrow},
        )
    ).json()
    assert overview["calls"] == 0
