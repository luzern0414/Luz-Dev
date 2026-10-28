"""长期记忆（SPEC §4.2）：Store 命名空间 (tenant_id, user_id, kind)，kind ∈ profile/preferences/facts。

允许：用户明确陈述的稳定偏好与称呼；禁止：支付信息、完整手机号/地址、一次性事实、模型推测（confidence < 0.8）。
同义合并：先在命名空间内找相似项（字符二元组 Jaccard ≥ 0.5），存在则更新而非新增。
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from langgraph.store.base import BaseStore

from agent_core import MemoryCandidate, utc_now

ALLOWED_KINDS: tuple[str, ...] = ("profile", "preferences", "facts")

FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),                 # 完整手机号
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),                 # 身份证
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),                    # 银行卡
    re.compile(r"密码|银行卡|卡号|支付宝账号|微信号|cvv", re.I),
    re.compile(r"(路|街|巷|号楼|单元|室)\s*\d"),              # 完整地址（含门牌）
)
ONE_TIME_WORDS: tuple[str, ...] = ("今天", "今晚", "现在", "这次", "一会儿", "待会", "明天")


def is_forbidden_memory(text: str) -> str | None:
    """返回拒绝原因；允许时返回 None。"""
    for p in FORBIDDEN_PATTERNS:
        if p.search(text):
            return "sensitive"
    if any(w in text for w in ONE_TIME_WORDS):
        return "one_time"
    return None


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text.lower())
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t}


def similarity(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


class MemoryStore:
    def __init__(self, store: BaseStore, *, min_confidence: float = 0.8, merge_threshold: float = 0.5,
                 ttl_days: int = 180, clock: Callable[[], datetime] = utc_now):
        self.store = store
        self.min_confidence = min_confidence
        self.merge_threshold = merge_threshold
        self.ttl_days = ttl_days
        self.clock = clock

    @staticmethod
    def namespace(tenant_id: str, user_id: str, kind: str) -> tuple[str, str, str]:
        return (str(tenant_id), str(user_id), kind)

    def write(self, tenant_id: str, user_id: str, cand: MemoryCandidate, source_turn: int = 0) -> str | None:
        """写入或合并；被规则拒绝时返回 None。"""
        if cand.kind not in ALLOWED_KINDS or cand.confidence < self.min_confidence:
            return None
        if is_forbidden_memory(cand.text):
            return None
        ns = self.namespace(tenant_id, user_id, cand.kind)
        now = self.clock()
        existing = self._find_similar(ns, cand.text)
        if existing is not None:
            key = existing["id"]
            value = {**existing, "text": cand.text, "confidence": max(cand.confidence, existing.get("confidence", 0)),
                     "source_turn": source_turn, "updated_at": now.isoformat(),
                     "expires_at": (now + timedelta(days=self.ttl_days)).isoformat()}
        else:
            key = "m_" + uuid.uuid4().hex[:12]
            value = {"id": key, "text": cand.text, "kind": cand.kind, "confidence": cand.confidence,
                     "source_turn": source_turn, "created_at": now.isoformat(), "updated_at": now.isoformat(),
                     "expires_at": (now + timedelta(days=self.ttl_days)).isoformat()}
        self.store.put(ns, key, value)
        return key

    def _find_similar(self, ns: tuple[str, str, str], text: str) -> dict[str, Any] | None:
        best, best_score = None, 0.0
        for item in self.store.search(ns, limit=100):
            score = similarity(item.value.get("text", ""), text)
            if score > best_score:
                best, best_score = item.value, score
        return best if best is not None and best_score >= self.merge_threshold else None

    def list(self, tenant_id: str, user_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        now = self.clock().isoformat()
        kinds = [kind] if kind else list(ALLOWED_KINDS)
        out: list[dict[str, Any]] = []
        for k in kinds:
            for item in self.store.search(self.namespace(tenant_id, user_id, k), limit=100):
                v = item.value
                if v.get("expires_at") and v["expires_at"] < now:
                    continue
                out.append({**v, "id": item.key, "kind": k})
        out.sort(key=lambda v: v.get("updated_at", ""), reverse=True)
        return out

    def delete(self, tenant_id: str, user_id: str, memory_id: str) -> bool:
        for k in ALLOWED_KINDS:
            ns = self.namespace(tenant_id, user_id, k)
            if self.store.get(ns, memory_id) is not None:
                self.store.delete(ns, memory_id)
                return True
        return False

    def search(self, tenant_id: str, user_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """按相似度排序返回；无相似项时按最近更新返回（偏好通常都值得注入）。"""
        items = self.list(tenant_id, user_id)
        scored = sorted(items, key=lambda v: similarity(v.get("text", ""), query), reverse=True)
        return scored[:limit]

    def known_preferences(self, tenant_id: str, user_id: str, query: str, limit: int = 5) -> list[str]:
        return [v["text"] for v in self.search(tenant_id, user_id, query, limit)]
