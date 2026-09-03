"""ORM 模型：7 张表（tech-plan 1.3）。JSON 字段落 TEXT（中文序列化配置见 app/db.py）。"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDatetime(TypeDecorator):
    """SQLite 的 DATETIME 往返会丢时区后缀（存的是无 offset 字符串）。

    读回统一补 UTC：全部列本就以 UTC 写入，补齐后 API 序列化带 +00:00，
    前端 new Date() 才能正确换算本地时区（否则日志时间会差一个时区）。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


def _updated_at() -> Mapped[datetime]:
    return mapped_column(UTCDatetime, default=utcnow, onupdate=utcnow)


class Base(DeclarativeBase):
    pass


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    protocol: Mapped[str] = mapped_column(String(30))  # openai_compatible | anthropic
    base_url: Mapped[str] = mapped_column(String(500))
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="")
    remark: Mapped[str] = mapped_column(String(500), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, default=utcnow)
    updated_at: Mapped[datetime] = _updated_at()


class LlmModel(Base):
    __tablename__ = "models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    upstream_model_id: Mapped[str] = mapped_column(String(200))
    # {temperature, max_tokens, top_p, timeout_seconds, max_retries}
    default_params: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, default=utcnow)
    updated_at: Mapped[datetime] = _updated_at()


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)  # 对外虚拟模型名
    strategy: Mapped[str] = mapped_column(String(30), default="council")
    judge_model_id: Mapped[int] = mapped_column(ForeignKey("models.id"))
    judge_prompt_template: Mapped[str] = mapped_column(Text, default="")
    member_timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    # {member_failure: skip|strict, judge_failure: degrade|strict}
    fault_tolerance: Mapped[dict] = mapped_column(JSON, default=dict)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, default=utcnow)
    updated_at: Mapped[datetime] = _updated_at()


class PipelineMember(Base):
    __tablename__ = "pipeline_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(
        ForeignKey("pipelines.id", ondelete="CASCADE"), index=True
    )
    model_id: Mapped[int] = mapped_column(ForeignKey("models.id"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    param_overrides: Mapped[dict] = mapped_column(JSON, default=dict)


class RequestLog(Base):
    __tablename__ = "request_logs"
    __table_args__ = (Index("ix_request_logs_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 即 request_id
    pipeline_name: Mapped[str] = mapped_column(String(100), default="")  # 命中的 Pipeline 名
    client_model_field: Mapped[str] = mapped_column(String(100))  # 客户端原始 model 字段
    request_messages: Mapped[list] = mapped_column(JSON, default=list)
    request_params: Mapped[dict] = mapped_column(JSON, default=dict)
    response_content: Mapped[str] = mapped_column(Text, default="")
    response_finish_reason: Mapped[str] = mapped_column(String(30), default="")
    # success | degraded | failed | client_cancelled
    status: Mapped[str] = mapped_column(String(30), default="success")
    total_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    total_prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    client_ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, default=utcnow)


class ModelCallLog(Base):
    __tablename__ = "model_call_logs"
    __table_args__ = (
        Index("ix_model_call_logs_request_id", "request_id"),
        Index("ix_model_call_logs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("request_logs.id"))
    role: Mapped[str] = mapped_column(String(20))  # member | judge | passthrough
    model_id: Mapped[int] = mapped_column(Integer, default=0)
    upstream_model_id: Mapped[str] = mapped_column(String(200), default="")
    provider_name: Mapped[str] = mapped_column(String(100), default="")
    request_payload: Mapped[dict] = mapped_column(JSON, default=dict)  # 实际发出的完整入参
    response_content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="success")  # success | failed | timeout
    error_message: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, default=utcnow)


class AppSetting(Base):
    """简单 KV：service_api_key_hash / fernet_key / log_retention_days 等。"""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, default=utcnow, onupdate=utcnow)
