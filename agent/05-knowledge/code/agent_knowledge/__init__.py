"""知识库（SPEC §5）：分块、检索协议（内存 BM25 兜底实现）、混合检索 RRF 合并、阈值、引用、有效期过滤、查询缓存。
生产用 pgvector + tsvector 实现同一 Retriever 协议。"""
from .chunking import Chunk, chunk_document, chunk_text
from .hybrid import rrf_merge
from .retriever import InMemoryBM25Retriever, Retriever, tokenize
from .service import KnowledgeService, normalize_query

__all__ = ["Chunk", "chunk_document", "chunk_text", "rrf_merge", "InMemoryBM25Retriever", "Retriever", "tokenize",
           "KnowledgeService", "normalize_query"]
