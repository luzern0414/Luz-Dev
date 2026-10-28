"""敏感字段加密（AES-256-GCM）与可查询哈希。

- 存储：手机号、身份证等写入 `*_enc`（密文）与 `*_hash`（HMAC-SHA256，用于等值查询与唯一约束）。
- 密钥：`LUZ_FIELD_KEY` 为 32 字节 base64；未配置时开发环境退化为可逆编码并打标记，生产启动拒绝。
- 密文格式：`v1:` + base64(nonce 12 字节 + 密文 + tag)。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

try:  # pragma: no cover - 可选依赖
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
except Exception:  # noqa: BLE001
    _AESGCM = None


class FieldCipher:
    def __init__(self, key_b64: str):
        self.dev_mode = not key_b64
        self._key = base64.b64decode(key_b64) if key_b64 else b"\x00" * 32
        if len(self._key) != 32:
            raise ValueError("LUZ_FIELD_KEY must decode to 32 bytes")

    def encrypt(self, plaintext: str | None) -> str | None:
        if plaintext is None:
            return None
        data = plaintext.encode()
        if self.dev_mode or _AESGCM is None:
            return "dev:" + base64.b64encode(data).decode()
        nonce = os.urandom(12)
        cipher = _AESGCM(self._key).encrypt(nonce, data, None)
        return "v1:" + base64.b64encode(nonce + cipher).decode()

    def decrypt(self, token: str | None) -> str | None:
        if token is None:
            return None
        if token.startswith("dev:"):
            return base64.b64decode(token[4:]).decode()
        if token.startswith("v1:"):
            if _AESGCM is None:
                raise RuntimeError("cryptography is required to decrypt v1 fields")
            raw = base64.b64decode(token[3:])
            return _AESGCM(self._key).decrypt(raw[:12], raw[12:], None).decode()
        raise ValueError("unknown ciphertext format")

    def lookup_hash(self, plaintext: str | None) -> str | None:
        if plaintext is None:
            return None
        return hmac.new(self._key, plaintext.encode(), hashlib.sha256).hexdigest()


def aes_gcm_decrypt(key: bytes, nonce: str, ciphertext_b64: str, associated_data: str) -> str:
    """微信支付 APIv3 回调与平台证书解密（AEAD_AES_256_GCM）。key 为 32 字节 APIv3Key。"""
    if _AESGCM is None:
        raise RuntimeError("cryptography is required for AES-GCM")
    if len(key) != 32:
        raise ValueError("APIv3 key must be 32 bytes")
    raw = base64.b64decode(ciphertext_b64)
    return _AESGCM(key).decrypt(nonce.encode(), raw, associated_data.encode()).decode()
