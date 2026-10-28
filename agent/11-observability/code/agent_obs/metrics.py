"""进程内指标：计数器 + 直方图（保留观测值列表，取 p50/p95）；每轮节点序列与耗时（TurnTrace）。
生产按 `TRACING_BACKEND` 导出到 LangSmith / OTel；本模块只提供统一入口，不绑定供应商。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


def _key(name: str, labels: dict[str, Any]) -> tuple:
    return (name, tuple(sorted((k, str(v)) for k, v in labels.items())))


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[tuple, float] = {}
        self._hist: dict[tuple, list[float]] = {}
        self._lock = threading.Lock()

    def inc(self, name: str, value: float = 1, **labels: Any) -> None:
        with self._lock:
            k = _key(name, labels)
            self._counters[k] = self._counters.get(k, 0) + value

    def observe(self, name: str, value: float, **labels: Any) -> None:
        with self._lock:
            self._hist.setdefault(_key(name, labels), []).append(float(value))

    def counter(self, name: str, **labels: Any) -> float:
        with self._lock:
            return self._counters.get(_key(name, labels), 0)

    def percentile(self, name: str, p: float, **labels: Any) -> float | None:
        with self._lock:
            values = sorted(self._hist.get(_key(name, labels), []))
        if not values:
            return None
        idx = min(len(values) - 1, int(round((p / 100) * (len(values) - 1))))
        return values[idx]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = {f"{n}{dict(l) if l else ''}": v for (n, l), v in self._counters.items()}
            hists = {f"{n}{dict(l) if l else ''}": len(v) for (n, l), v in self._hist.items()}
        return {"counters": counters, "histograms": hists}


@dataclass
class TurnTrace:
    """一轮内的节点序列与耗时；`span("plan")` 包住节点执行。"""
    trace_id: str
    clock: Callable[[], float] = time.monotonic
    spans: list[dict[str, Any]] = field(default_factory=list)
    attrs: dict[str, Any] = field(default_factory=dict)

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
        start = self.clock()
        record: dict[str, Any] = {"name": name, **attrs}
        try:
            yield record
        finally:
            record["duration_ms"] = int((self.clock() - start) * 1000)
            self.spans.append(record)

    def node_sequence(self) -> list[str]:
        return [s["name"] for s in self.spans]

    def set(self, **attrs: Any) -> None:
        self.attrs.update(attrs)

    def summary(self) -> dict[str, Any]:
        return {"trace_id": self.trace_id, "nodes": self.node_sequence(),
                "total_ms": sum(s.get("duration_ms", 0) for s in self.spans), **self.attrs}
