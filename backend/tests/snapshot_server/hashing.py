"""请求规范化与 hash：快照匹配的唯一依据。

规范：整个请求体 JSON 按 key 递归排序、紧凑序列化后取 sha256。
认证头不参与 hash；任何影响语义的字段都在 body 内。
"""

import hashlib
import json
from typing import Any


def canonical_json(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def request_hash(body: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
