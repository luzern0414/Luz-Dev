"""智能体核心：Pydantic 模型（SPEC §10）、配置（§16）、协议错误码（§8.3）。其他模块只依赖本包，本包不依赖其他模块。"""
from .config import AgentConfig, load_config
from .errors import (AgentException, ErrorCode, USER_MESSAGES, error_to_http_status,
                     make_agent_error)
from .models import (INTENT_IDS, AgentError, AgentEvent, Card, CardType, Channel, Citation,
                     ConfirmRequest, ConfirmResume, FinalAnswer, IntentId, IntentResult,
                     MemoryCandidate, MemoryKind, QuickReply, RetrievedChunk, RetrievedContext,
                     RiskLevel, Slot, ToolCall, ToolResult, TurnRequest, VerifyResult, Violation,
                     utc_now, utc_now_iso)

__all__ = [
    "AgentConfig", "load_config",
    "AgentException", "ErrorCode", "USER_MESSAGES", "error_to_http_status", "make_agent_error",
    "INTENT_IDS", "AgentError", "AgentEvent", "Card", "CardType", "Channel", "Citation",
    "ConfirmRequest", "ConfirmResume", "FinalAnswer", "IntentId", "IntentResult", "MemoryCandidate",
    "MemoryKind", "QuickReply", "RetrievedChunk", "RetrievedContext", "RiskLevel", "Slot", "ToolCall",
    "ToolResult", "TurnRequest", "VerifyResult", "Violation", "utc_now", "utc_now_iso",
]
