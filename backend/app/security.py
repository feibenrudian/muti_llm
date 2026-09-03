"""加密与安全：上游 API Key 的 Fernet 加解密、掩码显示、服务自身 Key 的生成与哈希校验。

Fernet 密钥由调用方传入（T05 首次启动生成并持久化于 app_settings.fernet_key）。
"""

import hashlib
import secrets

from cryptography.fernet import Fernet

SERVICE_KEY_PREFIX = "sk-local-"


def encrypt_secret(plaintext: str, fernet_key: bytes) -> str:
    """加密敏感字符串，返回可入库的密文 token。"""
    return Fernet(fernet_key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str, fernet_key: bytes) -> str:
    """解密 encrypt_secret 产生的密文。"""
    return Fernet(fernet_key).decrypt(ciphertext.encode("ascii")).decode("utf-8")


def generate_fernet_key() -> bytes:
    """生成新的 Fernet 密钥（首次启动/重置时用）。"""
    return Fernet.generate_key()


def mask_key(api_key: str) -> str:
    """对外只回显尾 4 位；过短的 Key 不泄露任何字符。"""
    if len(api_key) < 4:
        return "****"
    return "****" + api_key[-4:]


def mask_service_key(service_key: str) -> str:
    """设置页展示用：保留 sk-local- 前缀与尾 4 位，中间以 *** 代替（明文可一键复制）。"""
    if len(service_key) < 16:
        return "***" + service_key[-4:]
    return service_key[:10] + "***" + service_key[-4:]


def generate_service_key() -> str:
    """生成对外 API Key（客户端 Authorization: Bearer 用）。"""
    return SERVICE_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_service_key(service_key: str) -> str:
    """服务 Key 只存哈希（SHA-256），校验用恒定时间比较。"""
    return hashlib.sha256(service_key.encode("utf-8")).hexdigest()


def verify_service_key(service_key: str, stored_hash: str) -> bool:
    return secrets.compare_digest(hash_service_key(service_key), stored_hash)
