"""luzback：后端基础框架（配置、数据库、错误码、安全、幂等、发件箱、弹性、队列、审计、分页、任务、应用工厂）。"""
from .app import create_app, idempotency_key, optional_idempotency_key
from .audit import AuditLog, write_audit
from .config import Settings, get_settings, register_production_check, reload_settings
from .db import Base, BigId, BigIdMixin, SoftDeleteMixin, TimestampMixin, get_db, init_engine, transaction, utcnow
from .errors import AppError, ErrorCode, conflict, not_found, rule
from .idempotency import IdempotencyRecord, purge_idempotency, run_idempotent
from .outbox import OutboxEvent, OutboxWorker, ProcessedEvent, dispatch_pending, enqueue_event, mark_processed
from .export import cents_to_yuan, export_csv, safe_cell
from .listing import Field, ListSpec, escape_like
from .pagination import decode_cursor, encode_cursor, parse_limit, parse_page, parse_sort
from .resilience import Bulkhead, BreakerRegistry, CircuitBreaker, CircuitOpenError, TransientError, UpstreamError, resilient, retry_call
from .responses import cursor_page, offset_page, ok
from .security import create_token, decode_token, hash_password, mask_phone, verify_password
from .statemachine import StateMachine

__version__ = "2.0.0"

__all__ = [
    "create_app", "idempotency_key", "optional_idempotency_key",
    "AuditLog", "write_audit",
    "Settings", "get_settings", "register_production_check", "reload_settings",
    "Base", "BigId", "BigIdMixin", "SoftDeleteMixin", "TimestampMixin", "get_db", "init_engine", "transaction", "utcnow",
    "AppError", "ErrorCode", "conflict", "not_found", "rule",
    "IdempotencyRecord", "purge_idempotency", "run_idempotent",
    "OutboxEvent", "OutboxWorker", "ProcessedEvent", "dispatch_pending", "enqueue_event", "mark_processed",
    "cents_to_yuan", "export_csv", "safe_cell", "Field", "ListSpec", "escape_like",
    "decode_cursor", "encode_cursor", "parse_limit", "parse_page", "parse_sort",
    "Bulkhead", "BreakerRegistry", "CircuitBreaker", "CircuitOpenError", "TransientError", "UpstreamError", "resilient", "retry_call",
    "cursor_page", "offset_page", "ok",
    "create_token", "decode_token", "hash_password", "mask_phone", "verify_password",
    "StateMachine",
]
