"""UT-02-1..3：数据库层与 ORM。全部用内存 SQLite（StaticPool）。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import create_db_engine, create_session_factory, init_db
from app.orm import (
    LlmModel,
    ModelCallLog,
    Pipeline,
    PipelineMember,
    Provider,
    RequestLog,
)
from app.repos import Repository, get_setting, list_model_calls, set_setting


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_db_engine(":memory:")
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed_chain(session: AsyncSession) -> dict[str, int]:
    """建一条完整关联链 provider→model→pipeline→member→request→call，返回各表 id。"""
    provider = await Repository(session, Provider).create(
        name="DeepSeek", protocol="openai_compatible", base_url="https://api.deepseek.com"
    )
    model = await Repository(session, LlmModel).create(
        provider_id=provider.id,
        display_name="flash",
        upstream_model_id="deepseek-v4-flash",
        default_params={"temperature": 0.7, "timeout_seconds": 30},
    )
    pipeline = await Repository(session, Pipeline).create(
        name="council-v1",
        strategy="council",
        judge_model_id=model.id,
        judge_prompt_template="模板 {{candidate_answers}}",
        fault_tolerance={"member_failure": "skip"},
    )
    member = await Repository(session, PipelineMember).create(
        pipeline_id=pipeline.id, model_id=model.id, sort_order=0, param_overrides={"top_p": 0.9}
    )
    request_log = await Repository(session, RequestLog).create(
        pipeline_name="council-v1",
        client_model_field="council-v1",
        request_messages=[{"role": "user", "content": "hi"}],
        request_params={"temperature": 0.9},
        response_content="answer",
        status="success",
        total_duration_ms=123,
        total_prompt_tokens=10,
        total_completion_tokens=5,
    )
    call = await Repository(session, ModelCallLog).create(
        request_id=request_log.id,
        role="member",
        model_id=model.id,
        upstream_model_id="deepseek-v4-flash",
        provider_name="DeepSeek",
        request_payload={"model": "deepseek-v4-flash", "messages": []},
        response_content="member answer",
        status="success",
        duration_ms=100,
        prompt_tokens=5,
        completion_tokens=3,
    )
    await set_setting(session, "service_api_key_hash", "deadbeef")
    return {
        "provider": provider.id,
        "model": model.id,
        "pipeline": pipeline.id,
        "member": member.id,
        "request": request_log.id,
        "call": call.id,
    }


async def test_crud_roundtrip_all_tables(session: AsyncSession) -> None:
    """UT-02-1 各表 CRUD 往返：create 后字段可完整读回；update 生效；delete 后 get 为空。"""
    ids = await _seed_chain(session)

    provider_repo = Repository(session, Provider)
    got = await provider_repo.get(ids["provider"])
    assert got is not None
    assert got.name == "DeepSeek"
    assert got.protocol == "openai_compatible"
    assert got.base_url == "https://api.deepseek.com"
    assert got.enabled is True

    model_repo = Repository(session, LlmModel)
    got_model = await model_repo.get(ids["model"])
    assert got_model is not None
    assert got_model.default_params == {"temperature": 0.7, "timeout_seconds": 30}

    pipeline_repo = Repository(session, Pipeline)
    got_pipeline = await pipeline_repo.get(ids["pipeline"])
    assert got_pipeline is not None
    assert got_pipeline.fault_tolerance == {"member_failure": "skip"}
    assert got_pipeline.judge_prompt_template == "模板 {{candidate_answers}}"

    member_repo = Repository(session, PipelineMember)
    got_member = await member_repo.get(ids["member"])
    assert got_member is not None
    assert got_member.param_overrides == {"top_p": 0.9}

    request_repo = Repository(session, RequestLog)
    got_request = await request_repo.get(ids["request"])
    assert got_request is not None
    assert got_request.request_messages == [{"role": "user", "content": "hi"}]
    assert got_request.request_params == {"temperature": 0.9}
    assert got_request.status == "success"
    assert got_request.total_duration_ms == 123

    call_repo = Repository(session, ModelCallLog)
    got_call = await call_repo.get(ids["call"])
    assert got_call is not None
    assert got_call.role == "member"
    assert got_call.provider_name == "DeepSeek"
    assert got_call.prompt_tokens == 5

    assert await get_setting(session, "service_api_key_hash") == "deadbeef"
    await set_setting(session, "service_api_key_hash", "updated")
    assert await get_setting(session, "service_api_key_hash") == "updated"

    # update 生效
    updated = await provider_repo.update(ids["provider"], base_url="https://new.example.com")
    assert updated is not None and updated.base_url == "https://new.example.com"

    # delete 后 get 为空
    assert await member_repo.delete(ids["member"]) is True
    assert await member_repo.get(ids["member"]) is None
    assert await member_repo.delete(ids["member"]) is False

    # list 返回全部行（已 seed 的数据）
    assert len(await provider_repo.list()) == 1


async def test_pipeline_name_unique(session: AsyncSession) -> None:
    """UT-02-2 pipeline 名唯一约束：重名插入抛 IntegrityError。"""
    ids = await _seed_chain(session)
    with pytest.raises(IntegrityError):
        await Repository(session, Pipeline).create(
            name="council-v1",  # 与 seed 同名
            strategy="council",
            judge_model_id=ids["model"],
        )


async def test_model_calls_by_request_ordered(session: AsyncSession) -> None:
    """UT-02-3 model_call_logs 按 request_id 查询：父子关联正确、按时间排序。"""
    base = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
    request_repo = Repository(session, RequestLog)
    call_repo = Repository(session, ModelCallLog)

    r1 = await request_repo.create(client_model_field="council-v1")
    r2 = await request_repo.create(client_model_field="m2")

    # r1 的三次调用，故意乱序创建，时间递增
    for offset in [2, 0, 1]:
        await call_repo.create(
            request_id=r1.id,
            role="member",
            created_at=base + timedelta(seconds=offset),
            response_content=f"call-{offset}",
        )
    await call_repo.create(request_id=r2.id, role="member", response_content="other-request")

    calls = await list_model_calls(session, r1.id)
    assert len(calls) == 3
    assert [c.response_content for c in calls] == ["call-0", "call-1", "call-2"]
    assert all(c.request_id == r1.id for c in calls)
