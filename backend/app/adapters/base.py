"""适配器基座：统一调用契约（LlmRequest/LlmResult）、错误归一化（AdapterError）、重试（指数退避）。

策略层与网关只面对本模块的归一化类型，不接触各 SDK 的原生异常/结构。

超时语义（决策 D8）：上游一律流式调用，timeout_seconds 约束的是
"等待首个事件"与"事件间空闲"的时间，而非总时长——复杂问题只要持续产出就不会超时。
思考模型（DeepSeek/Qwen/Anthropic thinking）的 reasoning 增量也算"持续产出"：
思考阶段哪怕再长，只要增量在流就不会被判超时（否则思考型上游会被整体误杀）。
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
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
    # 工具透传（T43）：仅透传路径使用；anthropic 适配器暂不支持（非空即拒绝）
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None


@dataclass
class LlmUsage:
    """归一化 token 用量（T41 对账语义）。

    prompt_tokens 恒为计费输入总量（含缓存命中与写入）：OpenAI 兼容上游原生即此语义；
    Anthropic 的 input_tokens 不含缓存部分，由适配器补和归一化。cached_tokens 为命中
    （读缓存）部分，cache_write_tokens 为写入缓存部分（仅 Anthropic 暴露，OpenAI 自动
    缓存无独立写入计费）。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class StreamEvent:
    """流式事件：文本增量、思考增量（reasoning）、工具调用分片，或（流结束时）usage/finish_reason。

    reasoning 与 text 分离：思考内容绝不混入最终回答（complete/stream 只取 text），
    但同样计为"流上的活跃事件"，用于重置 TTFT/空闲超时计时器。
    tool_calls 为 OpenAI delta 分片形态（{index,id?,function:{name?,arguments?}} 原样透传）。
    """

    text: str = ""
    usage: LlmUsage | None = None
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None


@dataclass
class LlmResult:
    content: str
    usage: LlmUsage
    duration_ms: int
    # 工具调用（T43）：complete() 从流内分片按 index 聚合的完整 OpenAI tool_calls 形态
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str = "stop"


def merge_tool_call_deltas(
    fragments: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """按 index 合并流式 tool_calls 分片为完整调用（id/name 取首个，arguments 字符串拼接）。"""
    merged: dict[int, dict[str, Any]] = {}
    for frag in fragments:
        index = int(frag.get("index", 0))
        call = merged.setdefault(index, {"id": "", "type": "function", "function": {}})
        if frag.get("id"):
            call["id"] = frag["id"]
        if frag.get("type"):
            call["type"] = frag["type"]
        function = frag.get("function") or {}
        if function.get("name"):
            call["function"]["name"] = function["name"]
        if function.get("arguments"):
            call["function"]["arguments"] = call["function"].get("arguments", "") + function[
                "arguments"
            ]
    return merged


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
    timeout_seconds: float = 120.0
    max_retries: int = 1
    retry_base_delay: float = 0.5

    @abstractmethod
    def stream_events(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
        """流式调用：逐个 yield 文本增量事件，结束时（若上游支持）附一次 usage 事件。"""

    @abstractmethod
    async def probe(self) -> list[str]:
        """连通性探测：GET 上游模型列表，返回模型 ID 列表。

        不消耗对话 token，用于 Provider 管理界面验证 base_url/api_key；
        失败抛 AdapterError（连接/认证等错误已归一化）。
        """

    def _timed_events(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
        """给 stream_events 加超时：首事件前与事件间空闲均不得超过 timeout_seconds。

        生成持续多久都算正常，只有"等不到上游数据"才判超时（TTFT 语义）。
        首事件前的超时/失败可整体重试；已开始产出后的空闲超时不可重试（重放会重复生成）。
        """
        source = self.stream_events(request)
        timeout = self.timeout_seconds
        exhausted = object()  # anext 两参形式：耗尽返回哨兵（StopAsyncIteration 不能跨 task 传递）

        async def gen() -> AsyncIterator[StreamEvent]:
            got_event = False
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(
                            anext(source, exhausted), timeout=timeout
                        )
                    except TimeoutError:
                        stage = "首个响应" if not got_event else "后续数据"
                        raise AdapterError(
                            f"上游超时：{timeout:g}s 内未收到{stage}",
                            kind="timeout",
                            retryable=not got_event,
                        ) from None
                    if event is exhausted:
                        return
                    got_event = True
                    yield event
            finally:
                await source.aclose()

        return gen()

    def stream(self, request: LlmRequest) -> AsyncIterator[str]:
        """逐段 yield 增量文本（内部带 TTFT/空闲超时语义）。"""

        async def gen() -> AsyncIterator[str]:
            async for event in self._timed_events(request):
                if event.text:
                    yield event.text

        return gen()

    def stream_events_timed(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
        """带 TTFT/空闲超时语义的事件流（文本增量 + 结束时的 usage）。

        供需要"边流式消费、边拿 usage"的调用方使用（如管理端重跑裁判的 SSE）。
        """
        return self._timed_events(request)

    async def complete(self, request: LlmRequest) -> LlmResult:
        """流式调用聚合为完整结果。usage 取自流内 usage 事件（上游不支持则为 0）；
        tool_calls 按流内分片 index 聚合，finish_reason 取流内透传值（默认 stop）。"""
        start = time.perf_counter()
        parts: list[str] = []
        usage = LlmUsage()
        fragments: list[dict[str, Any]] = []
        finish_reason = "stop"
        for attempt in range(self.max_retries + 1):
            try:
                async for event in self._timed_events(request):
                    if event.text:
                        parts.append(event.text)
                    if event.tool_calls:
                        fragments.extend(event.tool_calls)
                    if event.finish_reason:
                        finish_reason = event.finish_reason
                    if event.usage is not None:
                        usage = event.usage
                merged = merge_tool_call_deltas(fragments)
                tool_calls = (
                    [merged[i] for i in sorted(merged)] if merged else None
                )
                return LlmResult(
                    content="".join(parts),
                    usage=usage,
                    duration_ms=elapsed_ms(start),
                    tool_calls=tool_calls,
                    finish_reason=finish_reason,
                )
            except AdapterError as err:
                # 已产出部分内容后不再整体重试（重放会重复生成）；首事件前的失败才可重试
                if not err.retryable or parts or attempt == self.max_retries:
                    raise
                delay = self.retry_base_delay * (2**attempt)
                if delay > 0:
                    await asyncio.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _with_retry(self, fn: Callable[[], Any]) -> Any:
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
