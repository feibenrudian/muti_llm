"""UT-51-1：登录失败锁定（进程内计数窗口，打桩时钟）。"""

import pytest

from app.auth_svc import (
    LOCK_SECONDS,
    LOCK_THRESHOLD,
    _lock_state,
    _lockout_remaining,
    _record_login_failure,
    reset_login_failures,
)


@pytest.fixture(autouse=True)
def _clean_lock_state():
    _lock_state.clear()
    yield
    _lock_state.clear()


def test_lockout_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT-51-1：5 次失败后锁定；窗口内正确口令也拒（文案由 login 层给）；窗口过后恢复。"""
    times = iter(float(i) for i in range(LOCK_THRESHOLD))
    monkeypatch.setattr("app.auth_svc.time.monotonic", lambda: next(times))

    for _ in range(LOCK_THRESHOLD - 1):
        _record_login_failure("alice", 0.0)
        assert _lockout_remaining("alice", 0.0) == 0  # 未达阈值
    _record_login_failure("alice", 0.0)
    remaining = _lockout_remaining("alice", 0.0)
    assert 0 < remaining <= LOCK_SECONDS

    # 窗口过后恢复：最后一个失败时间前移 LOCK_SECONDS 以上
    assert _lockout_remaining("alice", LOCK_THRESHOLD - 1 + LOCK_SECONDS + 1) == 0


def test_lockout_reset_on_success() -> None:
    """UT-51-1b：成功登录清零计数（reset_login_failures）。"""
    for i in range(LOCK_THRESHOLD - 1):
        _record_login_failure("bob", float(i))
    reset_login_failures("bob")
    assert _lockout_remaining("bob", float(LOCK_THRESHOLD)) == 0
