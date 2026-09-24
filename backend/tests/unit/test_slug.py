"""UT-42-1：供应商 slug 派生规则（derive_slug 纯函数）。"""

import pytest

from app.repos import derive_slug


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("GLM", "glm"),
        ("SRS Provider", "srs-provider"),
        ("  DeepSeek  官方  ", "deepseek"),  # 中文段被剔除后折连字符、去首尾
        ("glm@#4.6!", "glm-4-6"),
    ],
)
def test_slug_from_name(name: str, expected: str) -> None:
    """UT-42-1 名字归一：小写、非字母数字转连字符、折叠去首尾。"""
    assert derive_slug(name, set(), fallback="provider-0") == expected


def test_slug_conflict_appends_serial() -> None:
    """UT-42-2 查重：被占用时加序号 -2、-3 递增。"""
    taken = {"glm", "glm-2"}
    assert derive_slug("GLM", taken, fallback="provider-0") == "glm-3"


def test_slug_fallback_for_cjk() -> None:
    """UT-42-3 纯中文名派生不出 → 用 fallback；fallback 也被占用则同样加序号。"""
    assert derive_slug("智谱", set(), fallback="provider-7") == "provider-7"
    assert derive_slug("智谱", {"provider-7"}, fallback="provider-7") == "provider-7-2"
