"""适配器基座：统一调用契约（LlmRequest/LlmResult）、错误归一化（AdapterError）、重试（指数退避）。

策略层与网关只面对本模块的归一化类型，不接触各 SDK 的原生异常/结构。
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class NormalizedMessage:
    role: str  # system | user | assistant（校验在网关层完成）
    content: str


@dataclass
class LlmRequest:
    model: str  # 上游模型 ID
    messages: list[NormalizedMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None


@dataclass
class LlmUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LlmResult:
    content: str
    usage: LlmUsage
    duration_ms: int


class AdapterError(Exception):
    """上游调用失败的归一化错误。

    kind: timeout | connection | rate_limit | auth | bad_request | upstream_error
    retryable: 基座重试只针对 retryable 的错误（连接/超时/限流/5xx）。
    """

    def __init__(self, message: str, *, kind: str, retryable: bool, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.detail = detail


class BaseAdapter(ABC):
    timeout_seconds: float = 60.0
    max_retries: int = 1
    retry_base_delay: float = 0.5

    @abstractmethod
    async def complete(self, request: LlmRequest) -> LlmResult:
        """非流式调用，返回完整答案与 usage。"""

    @abstractmethod
    def stream(self, request: LlmRequest) -> AsyncIterator[str]:
        """流式调用，逐段 yield 增量文本（不含 usage；建立连接失败会抛 AdapterError）。"""

    @abstractmethod
    async def probe(self) -> list[str]:
        """连通性探测：GET 上游模型列表，返回模型 ID 列表。

        不消耗对话 token，用于 Provider 管理界面验证 base_url/api_key；
        失败抛 AdapterError（连接/认证等错误已归一化）。
        """

    async def _with_retry(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        last_error: AdapterError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await fn()
            except AdapterError as err:
                last_error = err
                if not err.retryable or attempt == self.max_retries:
                    raise
                delay = self.retry_base_delay * (2**attempt)
                if delay > 0:
                    await asyncio.sleep(delay)
        raise last_error  # pragma: no cover


def elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
