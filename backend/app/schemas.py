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
    slug: str
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
    upstream_missing: bool
    created_at: datetime
    updated_at: datetime


# ---- Pipeline ---------------------------------------------------------------------


class PipelineMemberIn(BaseModel):
    model_id: int
    param_overrides: dict[str, Any] = Field(default_factory=dict)


class PipelineCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9-_]+$", min_length=1, max_length=100)
    strategy: str = "council"
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    judge_model_id: int
    judge_prompt_template: str = ""
    member_timeout_seconds: int = Field(default=120, ge=1, le=600)
    fault_tolerance: dict[str, Any] = Field(default_factory=dict)
    max_concurrency: int = Field(default=10, ge=1, le=100)
    enabled: bool = True
    stream_process: bool = False
    tool_aggregation: bool = False
    members: list[PipelineMemberIn] = Field(min_length=1)


class PipelineUpdate(BaseModel):
    name: str | None = Field(default=None, pattern=r"^[a-z0-9-_]+$", min_length=1, max_length=100)
    strategy: str | None = None
    strategy_params: dict[str, Any] | None = None
    judge_model_id: int | None = None
    judge_prompt_template: str | None = None
    member_timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    fault_tolerance: dict[str, Any] | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=100)
    enabled: bool | None = None
    stream_process: bool | None = None
    tool_aggregation: bool | None = None
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
    strategy_params: dict[str, Any]
    judge_model_id: int
    judge_prompt_template: str
    member_timeout_seconds: int
    fault_tolerance: dict[str, Any]
    max_concurrency: int
    enabled: bool
    stream_process: bool
    tool_aggregation: bool
    created_at: datetime
    updated_at: datetime
    members: list[PipelineMemberOut]


# ---- 多用户：用户 / API Key / 授权（T47）-------------------------------------------


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9._-]+$")
    display_name: str = Field(default="", max_length=50)
    role: Literal["admin", "user"] = "user"
    remark: str = Field(default="", max_length=500)
    # role=admin 时必填；一般用户可选（设置后即可登录自助门户，T52）
    password: str | None = Field(default=None, min_length=8, max_length=200)


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=50)
    enabled: bool | None = None
    role: Literal["admin", "user"] | None = None
    remark: str | None = Field(default=None, max_length=500)
    # 提升为 admin 时必填（为其设置初始登录口令）
    password: str | None = Field(default=None, min_length=8, max_length=200)


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    enabled: bool
    remark: str
    key_count: int = 0
    last_activity_at: datetime | None = None
    created_at: datetime


class UserPasswordReset(BaseModel):
    new_password: str = Field(min_length=8, max_length=200)


class KeyCreate(BaseModel):
    name: str = Field(default="", max_length=50)


class KeyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    enabled: bool | None = None


class KeyOut(BaseModel):
    id: int
    name: str
    masked: str
    enabled: bool
    is_legacy: bool = False
    created_by: int = 0
    last_used_at: datetime | None = None
    created_at: datetime


class KeyCreatedOut(KeyOut):
    """创建响应：明文仅此一次返回（之后仅掩码 + /secret 主动复制）。"""

    api_key: str


class GrantsOut(BaseModel):
    pipelines: list[int] = Field(default_factory=list)
    models: list[int] = Field(default_factory=list)
    providers: list[int] = Field(default_factory=list)


GrantsPayload = GrantsOut  # PUT 授权请求与 GET 响应同形（覆盖式提交）
