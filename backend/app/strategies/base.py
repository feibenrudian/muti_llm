"""策略引擎：Strategy 接口、执行结果结构、注册表（新策略以插件注册，不改网关）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.adapters.base import BaseAdapter, LlmRequest, LlmUsage
from app.orm import LlmModel, Pipeline, PipelineMember, Provider


class StrategyNotFound(KeyError):
    """未注册的策略名。"""


class StrategyExecutionError(Exception):
    """策略执行失败（成员全败 / strict 裁判失败）；携带已完成调用的明细供网关落日志。"""

    def __init__(
        self,
        message: str,
        *,
        members: list[CallOutcome] | None = None,
        judge: CallOutcome | None = None,
    ) -> None:
        super().__init__(message)
        self.members = members or []
        self.judge = judge


class AllMembersFailed(StrategyExecutionError):
    """全部成员失败（或 strict 模式下任一失败）；message 含各成员错误摘要。"""


@dataclass
class MemberSpec:
    """一个成员的完整配置（ORM 行的内存快照，按配置顺序）。"""

    member: PipelineMember
    model: LlmModel
    provider: Provider


@dataclass
class StrategyContext:
    """策略执行上下文：网关解析请求与配置后组装。"""

    body: dict[str, Any]  # 原始请求体（含 messages/model/stream）
    user_params: dict[str, Any]  # 请求级采样参数（temperature/max_tokens/top_p）
    pipeline: Pipeline
    members: list[MemberSpec]
    judge_model: LlmModel
    judge_provider: Provider
    fernet_key: bytes


@dataclass
class CallOutcome:
    """一次上游调用的明细（网关据此写 model_call_logs）。"""

    role: str  # member | judge
    model_id: int
    upstream_model_id: str
    provider_name: str
    request_payload: dict[str, Any]
    response_content: str = ""
    status: str = "success"  # success | failed | timeout | client_cancelled
    error_message: str = ""
    duration_ms: int = 0
    usage: LlmUsage = field(default_factory=LlmUsage)

    def to_log_kwargs(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "model_id": self.model_id,
            "upstream_model_id": self.upstream_model_id,
            "provider_name": self.provider_name,
            "request_payload": self.request_payload,
            "response_content": self.response_content,
            "status": self.status,
            "error_message": self.error_message,
            "duration_ms": self.duration_ms,
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
        }


@dataclass
class StrategyResult:
    """非流式策略执行结果。"""

    final_content: str
    usage: LlmUsage  # 全部调用 token 汇总
    degraded: bool = False
    members: list[CallOutcome] = field(default_factory=list)
    judge: CallOutcome | None = None
    final_finish_reason: str = "stop"


@dataclass
class StreamPlan:
    """流式执行计划：策略先完成前置阶段（如成员并发），网关再流式消费最终阶段。"""

    adapter: BaseAdapter
    request: LlmRequest
    payload: dict[str, Any]  # 最终阶段实际请求体（入日志）
    model_id: int
    upstream_model_id: str
    provider_name: str
    pre_outcomes: list[CallOutcome]  # 前置阶段明细（成员）
    base_usage: LlmUsage  # 前置阶段 token 汇总


class Strategy(ABC):
    """聚合策略接口。新策略继承本类并用 register_strategy 注册。"""

    name: str = "abstract"

    @abstractmethod
    async def run(self, ctx: StrategyContext) -> StrategyResult:
        """非流式：产出最终答案与全部调用明细。"""

    @abstractmethod
    async def prepare_stream(self, ctx: StrategyContext) -> StreamPlan:
        """流式：完成前置阶段并给出最终阶段的流式调用计划。"""


_REGISTRY: dict[str, type[Strategy]] = {}


def register_strategy(strategy_cls: type[Strategy]) -> type[Strategy]:
    if not strategy_cls.name or strategy_cls.name == "abstract":
        raise ValueError("strategy must define a non-default name")
    _REGISTRY[strategy_cls.name] = strategy_cls
    return strategy_cls


def get_strategy(name: str) -> type[Strategy]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise StrategyNotFound(f"strategy {name!r} is not registered") from None


def registered_strategies() -> list[str]:
    return sorted(_REGISTRY)
