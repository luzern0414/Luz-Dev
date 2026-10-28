"""记忆（SPEC §4）：进模型前裁剪、运行摘要（RemoveMessage 成对删除）、长期记忆（Store 命名空间、允许类型、用户删除）、
实体表（last_entities）供指代消解。"""
from .entities import extract_entities, merge_entities
from .extract import MemoryCandidates, extract_candidates
from .longterm import ALLOWED_KINDS, FORBIDDEN_PATTERNS, MemoryStore, is_forbidden_memory
from .summary import estimate_tokens, needs_summary, summarize_messages
from .trim import trim_for_model

__all__ = [
    "extract_entities", "merge_entities", "MemoryCandidates", "extract_candidates", "ALLOWED_KINDS",
    "FORBIDDEN_PATTERNS", "MemoryStore", "is_forbidden_memory", "estimate_tokens", "needs_summary",
    "summarize_messages", "trim_for_model",
]
