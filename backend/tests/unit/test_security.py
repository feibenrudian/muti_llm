"""UT-04-1..3：加密与安全模块。"""

from app.security import (
    decrypt_secret,
    encrypt_secret,
    generate_fernet_key,
    generate_service_key,
    hash_service_key,
    mask_key,
    verify_service_key,
)


def test_encrypt_decrypt_roundtrip() -> None:
    """UT-04-1 加解密往返：encrypt→密文不含明文→decrypt 还原。"""
    key = generate_fernet_key()
    plaintext = "sk-upstream-979fbfa0e6134b71"
    ciphertext = encrypt_secret(plaintext, key)
    assert isinstance(ciphertext, str)
    assert plaintext not in ciphertext
    assert decrypt_secret(ciphertext, key) == plaintext


def test_mask_key() -> None:
    """UT-04-2 mask：sk-abc12345678 → ****5678；短 Key 不泄露字符。"""
    assert mask_key("sk-abc12345678") == "****5678"
    assert mask_key("abc") == "****"
    assert mask_key("") == "****"


def test_service_key_generate_and_verify() -> None:
    """UT-04-3 服务Key校验：正确 Key 通过；错误 Key 拒绝；两次生成不同。"""
    key = generate_service_key()
    assert key.startswith("sk-local-")

    stored_hash = hash_service_key(key)
    assert stored_hash != key  # 哈希与明文不同
    assert verify_service_key(key, stored_hash)
    assert not verify_service_key("sk-local-wrong", stored_hash)

    assert generate_service_key() != generate_service_key()
