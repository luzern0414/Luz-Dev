"""消息推送服务器：URL 验证签名、安全模式 AES 加解密、报文解析、事件分发与去重。

官方规则（server-ability/message-push）：
- URL 验证：sha1(sort([token, timestamp, nonce]) 拼接) == signature，通过后原样返回 echostr。
- 安全模式：msg_signature = sha1(sort([token, timestamp, nonce, Encrypt]) 拼接)；必须用 msg_signature 验签。
- AESKey = Base64_Decode(EncodingAESKey + "=")（32 字节）；AES-256-CBC，IV 取 AESKey 前 16 字节；
  PKCS#7 填充按 32 字节分块；明文 = random(16B) + msg_len(4B 网络字节序) + msg + appid，需校验 appid。
- 回包：空串或 "success"；需要加密回包时为 {Encrypt, MsgSignature, TimeStamp, Nonce}。
- JSON 格式下 List 只有一项时是对象、多项时是数组，解析时两种都兼容（normalize_list）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode

from .models import MpPushEvent

BLOCK = 32


def _sha1_sorted(*parts: str) -> str:
    return hashlib.sha1("".join(sorted(parts)).encode("utf-8")).hexdigest()


def url_signature(token: str, timestamp: str, nonce: str) -> str:
    return _sha1_sorted(token, timestamp, nonce)


def verify_url_signature(token: str, signature: str, timestamp: str, nonce: str) -> bool:
    if not token or not signature:
        return False
    return hmac.compare_digest(url_signature(token, timestamp, nonce), signature)


def msg_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    return _sha1_sorted(token, timestamp, nonce, encrypt)


def pkcs7_pad(data: bytes, block: int = BLOCK) -> bytes:
    n = block - len(data) % block
    return data + bytes([n]) * n


def pkcs7_unpad(data: bytes, block: int = BLOCK) -> bytes:
    if not data:
        raise ValueError("empty plaintext")
    n = data[-1]
    if n < 1 or n > block or data[-n:] != bytes([n]) * n:
        raise ValueError("bad padding")
    return data[:-n]


class PushCrypto:
    """WXBizMsgCrypt 等价实现（AES-256-CBC）。"""

    def __init__(self, token: str, encoding_aes_key: str, appid: str):
        if len(encoding_aes_key) != 43:
            raise ValueError("EncodingAESKey must be 43 characters")
        self.token = token
        self.key = base64.b64decode(encoding_aes_key + "=")
        if len(self.key) != 32:
            raise ValueError("EncodingAESKey must decode to 32 bytes")
        self.iv = self.key[:16]
        self.appid = appid

    def _cipher(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        return Cipher(algorithms.AES(self.key), modes.CBC(self.iv))

    def encrypt(self, plaintext: str, random16: bytes | None = None) -> str:
        msg = plaintext.encode("utf-8")
        raw = (random16 or os.urandom(16)) + struct.pack(">I", len(msg)) + msg + self.appid.encode("utf-8")
        enc = self._cipher().encryptor()
        return base64.b64encode(enc.update(pkcs7_pad(raw)) + enc.finalize()).decode()

    def decrypt(self, encrypt_b64: str) -> str:
        try:
            dec = self._cipher().decryptor()
            raw = pkcs7_unpad(dec.update(base64.b64decode(encrypt_b64)) + dec.finalize())
            length = struct.unpack(">I", raw[16:20])[0]
            msg, appid = raw[20:20 + length], raw[20 + length:]
        except Exception as exc:  # noqa: BLE001
            raise AppError(ErrorCode.UNAUTHORIZED, "cannot decrypt push message") from exc
        if appid.decode("utf-8", "replace") != self.appid:
            raise AppError(ErrorCode.UNAUTHORIZED, "push message appid mismatch")
        return msg.decode("utf-8")

    def verify_and_decrypt(self, msg_sig: str, timestamp: str, nonce: str, encrypt: str) -> str:
        expected = msg_signature(self.token, timestamp, nonce, encrypt)
        if not msg_sig or not hmac.compare_digest(expected, msg_sig):
            raise AppError(ErrorCode.UNAUTHORIZED, "bad push msg_signature")
        return self.decrypt(encrypt)

    def encrypt_reply(self, plaintext: str, timestamp: str | None = None, nonce: str | None = None,
                      random16: bytes | None = None) -> dict[str, Any]:
        timestamp = timestamp or str(int(time.time()))
        nonce = nonce or base64.b32encode(os.urandom(5)).decode().lower()
        encrypt = self.encrypt(plaintext, random16)
        return {"Encrypt": encrypt, "MsgSignature": msg_signature(self.token, timestamp, nonce, encrypt),
                "TimeStamp": int(timestamp), "Nonce": nonce}


# ---------------------------------------------------------------- 报文解析

def _xml_to_dict(node: ET.Element) -> Any:
    children = list(node)
    if not children:
        return (node.text or "").strip()
    out: dict[str, Any] = {}
    for child in children:
        value = _xml_to_dict(child)
        if child.tag in out:
            if not isinstance(out[child.tag], list):
                out[child.tag] = [out[child.tag]]
            out[child.tag].append(value)
        else:
            out[child.tag] = value
    return out


def parse_body(raw: bytes | str) -> dict[str, Any]:
    """JSON 或 XML（<xml>…</xml>）→ dict。XML 不解析 DTD 实体（ElementTree 不加载外部实体）。"""
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    text = text.strip()
    if not text:
        raise AppError(ErrorCode.BAD_REQUEST, "empty push body")
    if text.startswith("<"):
        if "<!DOCTYPE" in text or "<!ENTITY" in text:
            raise AppError(ErrorCode.BAD_REQUEST, "DTD not allowed")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise AppError(ErrorCode.BAD_REQUEST, "invalid xml") from exc
        data = _xml_to_dict(root)
        return data if isinstance(data, dict) else {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid json") from exc
    if not isinstance(data, dict):
        raise AppError(ErrorCode.BAD_REQUEST, "push body must be an object")
    return data


def normalize_list(value: Any) -> list[Any]:
    """JSON 推送里 List 一项时是对象、多项时是数组；XML 里同名多节点。统一成列表。"""
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MediaCheckEvent:
    """wxa_media_check：异步媒体检测结果（30 分钟内推送）。errcode 为 0 时结果才有效。"""

    trace_id: str
    appid: str
    errcode: int
    suggest: str
    label: int

    @property
    def valid(self) -> bool:
        return self.errcode == 0

    @property
    def passed(self) -> bool:
        return self.valid and self.suggest == "pass"

    @property
    def risky(self) -> bool:
        return self.valid and self.suggest == "risky"


def parse_media_check_event(message: dict[str, Any]) -> MediaCheckEvent:
    if message.get("Event") != "wxa_media_check":
        raise AppError(ErrorCode.BAD_REQUEST, "not a wxa_media_check event")
    result = message.get("result") or {}
    if isinstance(result, str):
        result = {}
    return MediaCheckEvent(trace_id=str(message.get("trace_id", "")), appid=str(message.get("appid", "")),
                           errcode=_int(message.get("errcode"), -1), suggest=str(result.get("suggest", "")),
                           label=_int(result.get("label")))


def dedup_key(message: dict[str, Any]) -> str:
    """去重键：消息有 MsgId 用 MsgId；事件用 发送方 + 时间 + 事件 (+ trace_id / MsgID)。"""
    if message.get("MsgId"):
        return f"msg:{message['MsgId']}"
    parts = [str(message.get("FromUserName", "")), str(message.get("CreateTime", "")), str(message.get("Event", ""))]
    for extra in ("trace_id", "MsgID"):
        if message.get(extra):
            parts.append(str(message[extra]))
    return "evt:" + hashlib.sha256("|".join(parts).encode()).hexdigest()


# ---------------------------------------------------------------- 分发

PushHandler = Callable[[Session, dict[str, Any]], None]
_handlers: dict[str, list[PushHandler]] = {}


def register_push_handler(event: str, handler: PushHandler) -> None:
    """按事件名（Event，普通消息用 MsgType）注册处理函数；同一函数重复注册只保留一次。处理函数不 commit。"""
    items = _handlers.setdefault(event, [])
    if handler not in items:
        items.append(handler)


def unregister_push_handler(event: str, handler: PushHandler) -> None:
    if handler in _handlers.get(event, []):
        _handlers[event].remove(handler)


def dispatch(db: Session, message: dict[str, Any]) -> str:
    """去重 → 调用处理函数 → 提交。返回 processed | duplicate | ignored。处理函数异常回滚并上抛（平台会重推）。"""
    name = str(message.get("Event") or message.get("MsgType") or "")
    handlers = list(_handlers.get(name, []))
    db.add(MpPushEvent(dedup_key=dedup_key(message), event=name[:64]))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return "duplicate"
    try:
        for handler in handlers:
            handler(db, message)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return "processed" if handlers else "ignored"
