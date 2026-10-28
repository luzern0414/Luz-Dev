"""消息推送回调路由：GET 为 URL 验证，POST 为事件推送。业务在 push.py；这里只做验签、解密与分发。

路径 /api/v1/webhooks/wechat-mp/push（SPEC §3.2 回调路径约定）；成功回 "success" 纯文本（官方要求）。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from luzback import AppError, ErrorCode, get_db

from . import push
from .config import get_config

router = APIRouter(prefix="/api/v1/webhooks/wechat-mp", tags=["wechat_mp"])


def _check_skew(timestamp: str, max_skew: int) -> None:
    if max_skew <= 0:
        return
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, "bad timestamp") from exc
    if abs(time.time() - ts) > max_skew:
        raise AppError(ErrorCode.UNAUTHORIZED, "push timestamp out of range")


@router.get("/push", response_class=PlainTextResponse)
def verify_url(signature: str = Query(""), timestamp: str = Query(""), nonce: str = Query(""), echostr: str = Query("")):
    config = get_config()
    if not push.verify_url_signature(config.push_token, signature, timestamp, nonce):
        raise AppError(ErrorCode.UNAUTHORIZED, "bad signature")
    return PlainTextResponse(echostr)


@router.post("/push", response_class=PlainTextResponse)
async def receive(request: Request, db=Depends(get_db)):
    config = get_config()
    q = request.query_params
    timestamp, nonce = q.get("timestamp", ""), q.get("nonce", "")
    if not push.verify_url_signature(config.push_token, q.get("signature", ""), timestamp, nonce):
        raise AppError(ErrorCode.UNAUTHORIZED, "bad signature")
    _check_skew(timestamp, config.push_max_skew_s)
    raw = await request.body()
    if q.get("encrypt_type") == "aes":
        if not config.push_aes_key:
            raise AppError(ErrorCode.UNAUTHORIZED, "encrypted push not configured")
        outer = push.parse_body(raw)
        crypto = push.PushCrypto(config.push_token, config.push_aes_key, config.appid)
        message = push.parse_body(crypto.verify_and_decrypt(q.get("msg_signature", ""), timestamp, nonce, str(outer.get("Encrypt", ""))))
    else:
        if config.push_require_aes and config.push_aes_key:
            raise AppError(ErrorCode.UNAUTHORIZED, "plaintext push rejected; security mode required")
        message = push.parse_body(raw)
    push.dispatch(db, message)
    return PlainTextResponse("success")
