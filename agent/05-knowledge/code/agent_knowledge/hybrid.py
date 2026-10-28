"""混合检索合并（SPEC §5.3）：向量 top-20 + 关键词 top-20 → RRF 合并。

合并后每个 chunk 的 `score` 保留各通道中的最高原始相似度（用于阈值判断），排序按 RRF 分。
"""
from __future__ import annotations

from agent_core import RetrievedChunk


def rrf_merge(ranked_lists: list[list[RetrievedChunk]], k: int = 60) -> list[RetrievedChunk]:
    rrf: dict[str, float] = {}
    best: dict[str, RetrievedChunk] = {}
    for ranked in ranked_lists:
        for rank, chunk in enumerate(ranked, start=1):
            key = f"{chunk.doc_id}#{chunk.chunk_id}"
            rrf[key] = rrf.get(key, 0.0) + 1.0 / (k + rank)
            if key not in best or chunk.score > best[key].score:
                best[key] = chunk
    order = sorted(rrf, key=lambda key: rrf[key], reverse=True)
    return [best[key] for key in order]
