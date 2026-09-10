"""应用配置（pydantic-settings，环境变量前缀 MUTILLM_）。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MUTILLM_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    database_path: str = "muti_llm.db"
    secret_key: str = ""
    log_retention_days: int = 30
    # SSE 心跳间隔秒数（空闲时发 `: ping` 注释行）；0 = 关闭
    sse_heartbeat_seconds: float = 15.0
    # 客户端断开后上游任务是否续跑到底（默认开）；关闭则取消上游并标记 client_cancelled
    detach_on_disconnect: bool = True


settings = Settings()
