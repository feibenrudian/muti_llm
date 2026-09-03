"""快照存储：tests/snapshots/ 下的 JSON 文件集合，按 request_hash 索引。

快照文件是真实 LLM 的固化输入输出（AGENTS.md 纪律：严禁手改内容）。
文件名格式：<scenario>__<hash前12位>.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.snapshot_server.hashing import request_hash

SNAPSHOT_SUFFIX = ".json"


@dataclass
class Snapshot:
    scenario: str
    request: dict[str, Any]
    non_stream_response: dict[str, Any] | None = None
    # 每项: {"chunk": <原始 OpenAI chunk 对象>, "delay_ms": <与上一 chunk 的间隔>}
    stream_chunks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def hash(self) -> str:
        return request_hash(self.request)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "request_hash": self.hash,
            "request": self.request,
            "non_stream_response": self.non_stream_response,
            "stream_chunks": self.stream_chunks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Snapshot:
        return cls(
            scenario=data["scenario"],
            request=data["request"],
            non_stream_response=data.get("non_stream_response"),
            stream_chunks=data.get("stream_chunks", []),
        )


class SnapshotStore:
    """快照目录的内存索引；save 即写盘并更新索引。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, Snapshot] = {}
        self.load()

    def load(self) -> None:
        self._index.clear()
        for path in sorted(self.root.glob(f"*{SNAPSHOT_SUFFIX}")):
            snap = Snapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
            self._index[snap.hash] = snap

    def get(self, request_hash: str) -> Snapshot | None:
        return self._index.get(request_hash)

    def save(self, snap: Snapshot) -> Path:
        digest = snap.hash.removeprefix("sha256:")[:12]
        path = self.root / f"{snap.scenario}__{digest}{SNAPSHOT_SUFFIX}"
        path.write_text(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        self._index[snap.hash] = snap
        return path

    def hashes(self) -> list[str]:
        return list(self._index)

    def model_ids(self) -> list[str]:
        """快照库中出现过的上游模型 ID（供 /v1/models 连通性探测端点返回）。"""
        return sorted(
            {str(s.request["model"]) for s in self._index.values() if s.request.get("model")}
        )

    def __len__(self) -> int:
        return len(self._index)
