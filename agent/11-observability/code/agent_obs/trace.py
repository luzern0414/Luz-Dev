"""trace_id 由网关生成并贯穿事件、工具调用、后端请求（X-Trace-Id）与日志。"""
from __future__ import annotations

import secrets


def new_trace_id() -> str:
    return "t_" + secrets.token_hex(12)


def new_request_id() -> str:
    return "r_" + secrets.token_hex(12)
