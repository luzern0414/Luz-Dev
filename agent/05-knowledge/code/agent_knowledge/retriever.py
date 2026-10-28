"""检索协议与内存 BM25 兜底实现（SPEC §5.3 关键词通道）。

分词：中文按字符二元组，英文/数字按词。分数归一化到 0–1（除以查询词理论最高分），与阈值可比。
同一 doc_id 重新入库产生新 version，旧版本标记失效（保留以便回滚）。
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Protocol

from agent_core import RetrievedChunk

from .chunking import Chunk, chunk_document

_TOKEN_RE = re.compile(r"[一-鿿]+|[a-z0-9]+(?:\.[0-9]+)?")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for run in _TOKEN_RE.findall(text.lower()):
        if re.match(r"[一-鿿]", run):
            if len(run) == 1:
                tokens.append(run)
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
        else:
            tokens.append(run)
    return tokens


class Retriever(Protocol):
    def search(self, tenant_id: str, query: str, top_k: int = 20, doc_type: str | None = None,
               now: str | None = None) -> list[RetrievedChunk]: ...


class InMemoryBM25Retriever:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self._chunks: dict[str, list[Chunk]] = defaultdict(list)       # tenant → chunks（含失效版本）
        self._tf: dict[str, Counter[str]] = {}                           # chunk key → tf
        self._versions: dict[tuple[str, str], int] = {}                  # (tenant, doc_id) → 当前版本

    @staticmethod
    def _key(tenant_id: str, chunk: Chunk) -> str:
        return f"{tenant_id}|{chunk.chunk_id}|v{chunk.version}"

    def add_document(self, tenant_id: str, doc_id: str, title_path: str, text: str, *, doc_type: str | None = None,
                     valid_from: str | None = None, valid_to: str | None = None) -> list[Chunk]:
        version = self._versions.get((tenant_id, doc_id), 0) + 1
        self._versions[(tenant_id, doc_id)] = version
        chunks = chunk_document(doc_id, title_path, text, doc_type=doc_type, valid_from=valid_from, valid_to=valid_to,
                                version=version)
        for c in chunks:
            c.meta["tenant_id"] = tenant_id
            self._chunks[tenant_id].append(c)
            self._tf[self._key(tenant_id, c)] = Counter(tokenize(c.text))
        return chunks

    def _active(self, tenant_id: str, doc_type: str | None, now: str | None) -> list[Chunk]:
        out = []
        for c in self._chunks.get(tenant_id, []):
            if c.version != self._versions.get((tenant_id, c.doc_id)):
                continue
            if doc_type and c.doc_type != doc_type:
                continue
            if now and ((c.valid_from and now < c.valid_from) or (c.valid_to and now > c.valid_to)):
                continue
            out.append(c)
        return out

    def search(self, tenant_id: str, query: str, top_k: int = 20, doc_type: str | None = None,
               now: str | None = None) -> list[RetrievedChunk]:
        docs = self._active(tenant_id, doc_type, now)
        q = tokenize(query)
        if not docs or not q:
            return []
        n = len(docs)
        tfs = [self._tf[self._key(tenant_id, c)] for c in docs]
        avgdl = sum(sum(tf.values()) for tf in tfs) / n
        df: Counter[str] = Counter()
        for tf in tfs:
            df.update(set(tf))
        q_terms = Counter(q)
        idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q_terms}
        max_score = sum(idf[t] * (self.k1 + 1) for t in q_terms) or 1.0
        scored: list[tuple[float, Chunk]] = []
        for c, tf in zip(docs, tfs):
            dl = sum(tf.values()) or 1
            s = 0.0
            for t in q_terms:
                f = tf.get(t, 0)
                if f:
                    s += idf[t] * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / avgdl))
            if s > 0:
                scored.append((s / max_score, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [RetrievedChunk(doc_id=c.doc_id, chunk_id=c.chunk_id, title_path=c.title_path, text=c.text,
                               score=round(s, 4), doc_type=c.doc_type) for s, c in scored[:top_k]]
