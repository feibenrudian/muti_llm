"""应用配置（pydantic-settings，环境变量前缀 MUTILLM_）。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MUTILLM_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    database_path: str = "muti_llm.db"
    secret_key: str = ""
    log_retention_days: int = 30


settings = Settings()
