"""适配器工厂：按 Provider 协议构造对应适配器；解密上游 Key。"""

from app.adapters.anthropic_adapter import AnthropicAdapter
from app.adapters.base import AdapterError, BaseAdapter
from app.adapters.openai_compat import OpenAICompatAdapter
from app.orm import Provider
from app.security import decrypt_secret


def decrypt_provider_key(api_key_encrypted: str, fernet_key: bytes) -> str:
    if not api_key_encrypted:
        return ""
    return decrypt_secret(api_key_encrypted, fernet_key)


def build_adapter(
    provider: Provider,
    *,
    fernet_key: bytes,
    timeout_seconds: float = 60.0,
    max_retries: int = 1,
    retry_base_delay: float = 0.5,
) -> BaseAdapter:
    api_key = decrypt_provider_key(provider.api_key_encrypted, fernet_key)
    common = {
        "api_key": api_key,
        "timeout_seconds": timeout_seconds,
        "max_retries": max_retries,
        "retry_base_delay": retry_base_delay,
    }
    if provider.protocol == "openai_compatible":
        return OpenAICompatAdapter(base_url=provider.base_url, **common)
    if provider.protocol == "anthropic":
        return AnthropicAdapter(base_url=provider.base_url, **common)
    raise AdapterError(f"未知协议: {provider.protocol!r}", kind="bad_request", retryable=False)
