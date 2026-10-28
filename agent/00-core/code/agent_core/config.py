"""配置项（SPEC §16），全部可由环境变量覆盖。`load_config(env)` 便于测试注入。"""
from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    agent_enabled: bool = True
    agent_write_enabled: bool = True
    llm_primary: str = "openai:gpt-4o-mini"
    llm_fallback: str | None = None
    llm_timeout_s: float = 20.0
    llm_first_token_timeout_s: float = 8.0
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_enabled: bool = False                 # 为 False 时不会发起任何外部模型请求（测试默认）
    embedding_model: str | None = None
    kb_topk: int = 6
    kb_min_score: float = 0.35
    kb_timeout_s: float = 1.5
    max_tool_calls: int = 5
    max_turn_tokens: int = 8000
    confirm_ttl_s: int = 600
    breaker_window_s: int = 60
    breaker_min_fails: int = 10
    breaker_fail_rate: float = 0.5
    breaker_open_s: int = 30
    breaker_half_open_probes: int = 3
    user_daily_turns: int = 200
    tenant_daily_budget_cents: int | None = None
    checkpoint_dsn: str | None = None
    store_dsn: str | None = None
    kb_dsn: str | None = None
    tracing_backend: str = "none"
    pii_redact: bool = True
    backend_base_url: str = "http://127.0.0.1:8000"
    service_name: str = "agent"
    service_secret: str = "change-me"
    sse_ping_interval_s: float = 15.0
    max_text_chars: int = 500
    dedup_ttl_s: int = 24 * 3600
    conversation_ttl_s: int = 30 * 24 * 3600
    tenant_id: str = Field(default="default", description="单租户部署时的默认租户")


_ENV_KEYS: dict[str, str] = {
    "AGENT_ENABLED": "agent_enabled",
    "AGENT_WRITE_ENABLED": "agent_write_enabled",
    "LLM_PRIMARY": "llm_primary",
    "LLM_FALLBACK": "llm_fallback",
    "LLM_TIMEOUT_S": "llm_timeout_s",
    "LLM_FIRST_TOKEN_TIMEOUT_S": "llm_first_token_timeout_s",
    "LLM_BASE_URL": "llm_base_url",
    "LLM_API_KEY": "llm_api_key",
    "LLM_ENABLED": "llm_enabled",
    "EMBEDDING_MODEL": "embedding_model",
    "KB_TOPK": "kb_topk",
    "KB_MIN_SCORE": "kb_min_score",
    "KB_TIMEOUT_S": "kb_timeout_s",
    "MAX_TOOL_CALLS": "max_tool_calls",
    "MAX_TURN_TOKENS": "max_turn_tokens",
    "CONFIRM_TTL_S": "confirm_ttl_s",
    "BREAKER_WINDOW_S": "breaker_window_s",
    "BREAKER_MIN_FAILS": "breaker_min_fails",
    "BREAKER_FAIL_RATE": "breaker_fail_rate",
    "BREAKER_OPEN_S": "breaker_open_s",
    "USER_DAILY_TURNS": "user_daily_turns",
    "TENANT_DAILY_BUDGET_CENTS": "tenant_daily_budget_cents",
    "CHECKPOINT_DSN": "checkpoint_dsn",
    "STORE_DSN": "store_dsn",
    "KB_DSN": "kb_dsn",
    "TRACING_BACKEND": "tracing_backend",
    "PII_REDACT": "pii_redact",
    "BACKEND_BASE_URL": "backend_base_url",
    "SERVICE_NAME": "service_name",
    "SERVICE_SECRET": "service_secret",
    "SSE_PING_INTERVAL_S": "sse_ping_interval_s",
    "AGENT_TENANT_ID": "tenant_id",
}

_TRUE = {"1", "true", "yes", "on"}


def load_config(env: Mapping[str, str] | None = None) -> AgentConfig:
    """从环境变量构造配置；未设置的项用默认值。布尔值接受 1/true/yes/on。"""
    source = os.environ if env is None else env
    values: dict[str, object] = {}
    for key, attr in _ENV_KEYS.items():
        if key not in source or source[key] == "":
            continue
        raw = source[key]
        kind = AgentConfig.model_fields[attr].annotation
        if kind is bool:
            values[attr] = raw.strip().lower() in _TRUE
        else:
            values[attr] = raw
    return AgentConfig(**values)
