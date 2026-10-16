"""UT-49-1..3：授权判定（AccessScope / 过滤函数，纯逻辑 + 打桩库）。"""

from dataclasses import dataclass
from pathlib import Path

from app.authz import (
    AccessScope,
    filter_models,
    filter_pipelines,
    load_scope,
    visible_provider_ids,
)
from app.db import create_db_engine, create_session_factory, init_db
from app.orm import User, UserGrant


@dataclass
class _Row:
    id: int
    provider_id: int = 1
    upstream_model_id: str = "m"


UNRESTRICTED = AccessScope(unrestricted=True)
SCOPED = AccessScope(
    unrestricted=False,
    pipeline_ids=frozenset({10}),
    model_ids=frozenset({2}),
    provider_ids=frozenset({100}),
)


def test_scope_matrix() -> None:
    """UT-49-2 判定矩阵：Pipeline/模型行/供应商级/动态行/零授权（方案 §3.4 表逐格）。"""
    # Pipeline
    assert SCOPED.allows_pipeline(10)
    assert not SCOPED.allows_pipeline(11)
    assert UNRESTRICTED.allows_pipeline(999)

    # 模型行：模型级命中 / 供应商级命中 / 都不命中
    assert SCOPED.allows_model_row(_Row(id=2))
    assert SCOPED.allows_model_row(_Row(id=5, provider_id=100))
    assert not SCOPED.allows_model_row(_Row(id=6, provider_id=200))

    # 动态行（库内无记录）：仅供应商级授权
    assert SCOPED.allows_dynamic(100)
    assert not SCOPED.allows_dynamic(200)
    assert UNRESTRICTED.allows_dynamic(1)

    # 零授权全拒
    empty = AccessScope(unrestricted=False)
    assert not empty.allows_pipeline(10)
    assert not empty.allows_model_row(_Row(id=2))
    assert not empty.allows_dynamic(100)


def test_filter_lists() -> None:
    """UT-49-3 列表过滤：混合数据（授权/未授权）正确；unrestricted 直通原列表对象。"""
    pipelines = [_Row(id=10), _Row(id=11), _Row(id=12)]
    assert [p.id for p in filter_pipelines(SCOPED, pipelines)] == [10]

    models = [_Row(id=1, provider_id=100), _Row(id=2, provider_id=300), _Row(id=3, provider_id=200)]
    assert [m.id for m in filter_models(SCOPED, models)] == [1, 2]

    # unrestricted 直通原列表（不重建 → 保证与升级前逐字节一致）
    assert filter_pipelines(UNRESTRICTED, pipelines) is pipelines
    assert filter_models(UNRESTRICTED, models) is models

    providers = [_Row(id=100), _Row(id=200), _Row(id=300)]
    # 200 可见因存在被授权模型 id=3? 不：id=3 属 200 未被授权；300 下模型 id=2 被授权 → 300 可见
    visible = visible_provider_ids(SCOPED, providers, models)  # type: ignore[arg-type]
    assert visible == {100, 300}
    assert visible_provider_ids(UNRESTRICTED, providers, models) == {100, 200, 300}  # type: ignore[arg-type]


async def test_load_scope(tmp_path: Path, monkeypatch: object = None) -> None:
    """UT-49-1 scope 装载：三类授权行 → 三个集合；管理员 unrestricted（不查授权表）。"""
    engine = create_db_engine(str(tmp_path / "a.db"))
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        alice = User(username="alice", role="user")
        admin = User(username="admin", role="admin")
        session.add_all([alice, admin])
        await session.flush()
        session.add_all(
            [
                UserGrant(user_id=alice.id, kind="pipeline", target_id=10),
                UserGrant(user_id=alice.id, kind="model", target_id=2),
                UserGrant(user_id=alice.id, kind="provider", target_id=100),
            ]
        )
        await session.commit()

        @dataclass
        class Caller:
            user_id: int
            role: str

        scoped = await load_scope(session, Caller(alice.id, "user"))
        assert scoped == SCOPED

        # 管理员短路：monkeypatch select 计数复杂，这里断言 unrestricted 且结果与授权表无关
        admin_scope = await load_scope(session, Caller(admin.id, "admin"))
        assert admin_scope.unrestricted
        assert admin_scope.allows_pipeline(99999)
    await engine.dispose()
