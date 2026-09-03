"""Pydantic 请求/响应模型：管理 API 与 OpenAI 兼容网关共用。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---- Provider ---------------------------------------------------------------------


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    protocol: Literal["openai_compatible", "anthropic"]
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = ""
    remark: str = ""
    enabled: bool = True


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    protocol: Literal["openai_compatible", "anthropic"] | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    api_key: str | None = None  # 提供则轮换（加密入库，不回显）
    remark: str | None = None
    enabled: bool | None = None


class ProviderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    protocol: str
    base_url: str
    api_key_masked: str
    remark: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


# ---- Model ------------------------------------------------------------------------


class ModelCreate(BaseModel):
    provider_id: int
    display_name: str = Field(min_length=1, max_length=100)
    upstream_model_id: str = Field(min_length=1, max_length=200)
    default_params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ModelUpdate(BaseModel):
    provider_id: int | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    upstream_model_id: str | None = Field(default=None, min_length=1, max_length=200)
    default_params: dict[str, Any] | None = None
    enabled: bool | None = None


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_id: int
    display_name: str
    upstream_model_id: str
    default_params: dict[str, Any]
    enabled: bool
    created_at: datetime
    updated_at: datetime


# ---- Pipeline ---------------------------------------------------------------------


class PipelineMemberIn(BaseModel):
    model_id: int
    param_overrides: dict[str, Any] = Field(default_factory=dict)


class PipelineCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9-_]+$", min_length=1, max_length=100)
    strategy: str = "council"
    judge_model_id: int
    judge_prompt_template: str = ""
    member_timeout_seconds: int = Field(default=120, ge=1, le=600)
    fault_tolerance: dict[str, Any] = Field(default_factory=dict)
    max_concurrency: int = Field(default=10, ge=1, le=100)
    enabled: bool = True
    members: list[PipelineMemberIn] = Field(min_length=1)


class PipelineUpdate(BaseModel):
    name: str | None = Field(default=None, pattern=r"^[a-z0-9-_]+$", min_length=1, max_length=100)
    strategy: str | None = None
    judge_model_id: int | None = None
    judge_prompt_template: str | None = None
    member_timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    fault_tolerance: dict[str, Any] | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=100)
    enabled: bool | None = None
    members: list[PipelineMemberIn] | None = None


class PipelineMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    model_id: int
    sort_order: int
    param_overrides: dict[str, Any]


class PipelineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    strategy: str
    judge_model_id: int
    judge_prompt_template: str
    member_timeout_seconds: int
    fault_tolerance: dict[str, Any]
    max_concurrency: int
    enabled: bool
    created_at: datetime
    updated_at: datetime
    members: list[PipelineMemberOut]
