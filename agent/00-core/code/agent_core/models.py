"""Pydantic 模型清单（SPEC §10）。前后端协议字段（shared/protocol.md）与本文件一致。

约定：
- 时间一律 ISO 8601 UTC 字符串（`utc_now_iso()`），便于进 checkpoint / 事件信封。
- 金额一律 `*_cents` 整数分。
- 模型输出里出现的身份字段（user_id/tenant_id）不在任何输入模型中，图节点一律忽略。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------- 公共 ----------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


Channel = Literal["miniprogram", "h5", "admin"]
RiskLevel = Literal["read", "low_write", "high", "forbidden", "disabled"]
CardType = Literal["product", "order", "store", "confirm", "handoff", "citation", "notice"]
MemoryKind = Literal["profile", "preferences", "facts"]

# 意图 id（SPEC §3.1）。`general` 为 <0.5 置信度时的兜底意图。
INTENT_IDS: tuple[str, ...] = (
    "greet_chitchat", "product_search", "product_compare", "cart_add", "order_create", "order_query",
    "order_modify", "after_sales", "member_query", "coupon_claim", "recharge", "store_info",
    "queue_booking", "policy_faq", "usage_help", "admin_report", "admin_draft", "handoff",
    "out_of_scope", "general",
)
IntentId = Literal[
    "greet_chitchat", "product_search", "product_compare", "cart_add", "order_create", "order_query",
    "order_modify", "after_sales", "member_query", "coupon_claim", "recharge", "store_info",
    "queue_booking", "policy_faq", "usage_help", "admin_report", "admin_draft", "handoff",
    "out_of_scope", "general",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------- 一轮输入 ----------

class TurnRequest(StrictModel):
    conversation_id: str = Field(min_length=1, max_length=64)
    turn_id: int | None = None
    text: str = Field(min_length=1, max_length=500)
    client_message_id: str = Field(min_length=1, max_length=64)
    channel: Channel = "miniprogram"
    locale: str = "zh-CN"
    entry_page: str | None = Field(default=None, max_length=64)
    entry_ref: str | None = Field(default=None, max_length=64)

    @field_validator("text")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("text must not be blank")
        return v

    @field_validator("client_message_id")
    @classmethod
    def _uuid(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError("client_message_id must be a uuid") from exc
        return v


# ---------- 意图 ----------

class Slot(StrictModel):
    name: str
    value: str | int | float | None = None
    source: Literal["user", "rule", "context", "default"] = "user"


class IntentResult(StrictModel):
    intent: IntentId
    confidence: float = Field(ge=0, le=1)
    slots: list[Slot] = []
    missing_required: list[str] = []
    secondary: IntentId | None = None
    reason: str = Field(default="", max_length=120)

    def slot(self, name: str) -> Slot | None:
        for s in self.slots:
            if s.name == name:
                return s
        return None


# ---------- 知识库 ----------

class RetrievedChunk(StrictModel):
    doc_id: str
    chunk_id: str
    title_path: str = ""
    text: str
    score: float = 0.0
    doc_type: str | None = None


class RetrievedContext(StrictModel):
    chunks: list[RetrievedChunk] = []
    hit: bool = False


# ---------- 工具 ----------

class ToolCall(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = {}
    reason: str = Field(default="", max_length=80)


class ToolResult(StrictModel):
    ok: bool
    data: Any = None
    error_code: str | None = None
    message: str | None = None
    retryable: bool = False
    latency_ms: int = 0
    sanitized: bool = False
    tool: str | None = None


# ---------- 确认 ----------

class ConfirmRequest(StrictModel):
    request_id: str
    action: str
    summary: str
    args_view: dict[str, Any] = {}
    amount_cents: int | None = None
    expires_at: str
    idempotency_key: str
    danger: bool = False


class ConfirmResume(StrictModel):
    request_id: str
    approved: bool
    edits: dict[str, Any] | None = None


# ---------- 回答 ----------

class Card(StrictModel):
    type: CardType
    payload: dict[str, Any] = {}
    route: str = Field(min_length=1)


class QuickReply(StrictModel):
    label: str = Field(min_length=1, max_length=12)
    send_text: str | None = None
    route: str | None = None


class Citation(StrictModel):
    doc_id: str
    chunk_id: str
    title_path: str = ""


class FinalAnswer(StrictModel):
    text: str = ""
    cards: list[Card] = []
    quick_replies: list[QuickReply] = Field(default_factory=list, max_length=4)
    citations: list[Citation] = []
    degraded: list[str] = []

    def has_entry(self) -> bool:
        return any(c.route for c in self.cards) or any(q.route for q in self.quick_replies)


class Violation(StrictModel):
    rule: str
    detail: str = ""


class VerifyResult(StrictModel):
    passed: bool
    violations: list[Violation] = []

    def feedback(self) -> str:
        return "；".join(f"{v.rule}: {v.detail}" for v in self.violations)


# ---------- 记忆 ----------

class MemoryCandidate(StrictModel):
    kind: MemoryKind
    text: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0, le=1)


# ---------- 事件与错误 ----------

class AgentEvent(StrictModel):
    seq: int
    turn_id: int
    event: str
    data: dict[str, Any] = {}
    ts: str = Field(default_factory=utc_now_iso)


class AgentError(StrictModel):
    code: str
    message: str
    retryable: bool = False
    retry_after: int | None = None
    trace_id: str = ""
