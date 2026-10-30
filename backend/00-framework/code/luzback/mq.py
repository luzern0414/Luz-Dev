"""消息队列抽象：发布 / 消费同一接口，内存实现用于开发与测试，Redis Streams 用于生产。

- 生产者：发件箱 worker 调用 broker.publish(topic, payload, key=event_id)。
- 消费者：consume(topic, group, handler) 按消费者组拉取；handler 抛错则不 ack，由 reclaim 重投；
  业务幂等由 outbox.mark_processed(consumer, event_id) 保证。
- 主题命名：<聚合>.<事件>（order.paid、queue.called）；payload 必含 event_id。
- 链路：发布时把 traceparent（与旧 X-Trace-Id）写进消息头，消费时用 continue_trace 接上，日志按同一个 trace_id 串起来。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .tracing import continue_trace, outgoing_headers


@dataclass
class Message:
    id: str
    topic: str
    payload: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    key: str | None = None
    attempts: int = 0


class Broker(Protocol):
    def publish(self, topic: str, payload: dict[str, Any], key: str | None = None, headers: dict[str, str] | None = None) -> str: ...
    def consume(self, topic: str, group: str, handler: Callable[[Message], None], count: int = 10, block_ms: int = 1000) -> int: ...


class InMemoryBroker:
    """每个主题一个列表；消费者组记录偏移与未确认消息；线程安全。"""

    def __init__(self):
        self._topics: dict[str, list[Message]] = {}
        self._offsets: dict[tuple[str, str], int] = {}
        self._pending: dict[tuple[str, str], list[Message]] = {}
        self._lock = threading.Lock()

    def publish(self, topic: str, payload: dict[str, Any], key: str | None = None, headers: dict[str, str] | None = None) -> str:
        message = Message(id=uuid.uuid4().hex, topic=topic, payload=dict(payload), headers=dict(headers or {}), key=key)
        with self._lock:
            self._topics.setdefault(topic, []).append(message)
        return message.id

    def consume(self, topic: str, group: str, handler: Callable[[Message], None], count: int = 10, block_ms: int = 1000) -> int:
        with self._lock:
            messages = self._topics.get(topic, [])
            start = self._offsets.get((topic, group), 0)
            batch = list(self._pending.get((topic, group), [])) + messages[start:start + count]
            self._offsets[(topic, group)] = min(len(messages), start + count)
            self._pending[(topic, group)] = []
        handled = 0
        for message in batch:
            message.attempts += 1
            try:
                _handle(handler, message)
                handled += 1
            except Exception:  # noqa: BLE001
                with self._lock:
                    self._pending.setdefault((topic, group), []).append(message)
        return handled

    def size(self, topic: str) -> int:
        with self._lock:
            return len(self._topics.get(topic, []))


class RedisStreamsBroker:  # pragma: no cover - 需要 Redis
    """XADD / XREADGROUP / XACK；XAUTOCLAIM 回收空闲超过 min_idle_ms 的未确认消息。"""

    def __init__(self, client, maxlen: int = 100_000, min_idle_ms: int = 60_000, consumer_name: str | None = None):
        self.client = client
        self.maxlen = maxlen
        self.min_idle_ms = min_idle_ms
        self.consumer = consumer_name or f"c-{uuid.uuid4().hex[:8]}"
        self._groups: set[tuple[str, str]] = set()

    def publish(self, topic: str, payload: dict[str, Any], key: str | None = None, headers: dict[str, str] | None = None) -> str:
        fields = {"payload": json.dumps(payload, default=str, ensure_ascii=False), "key": key or "", "headers": json.dumps(headers or {})}
        return self.client.xadd(topic, fields, maxlen=self.maxlen, approximate=True)

    def _ensure_group(self, topic: str, group: str) -> None:
        if (topic, group) in self._groups:
            return
        try:
            self.client.xgroup_create(topic, group, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001
            if "BUSYGROUP" not in str(exc):
                raise
        self._groups.add((topic, group))

    def consume(self, topic: str, group: str, handler: Callable[[Message], None], count: int = 10, block_ms: int = 1000) -> int:
        self._ensure_group(topic, group)
        handled = 0
        claimed = self.client.xautoclaim(topic, group, self.consumer, min_idle_time=self.min_idle_ms, start_id="0-0", count=count)
        entries = list(claimed[1]) if claimed and len(claimed) > 1 else []
        fresh = self.client.xreadgroup(group, self.consumer, {topic: ">"}, count=count, block=block_ms) or []
        for _stream, items in fresh:
            entries.extend(items)
        for message_id, fields in entries:
            raw = {k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v for k, v in fields.items()}
            message = Message(id=message_id.decode() if isinstance(message_id, bytes) else message_id, topic=topic,
                              payload=json.loads(raw.get("payload", "{}")), headers=json.loads(raw.get("headers", "{}")),
                              key=raw.get("key") or None)
            try:
                _handle(handler, message)
                self.client.xack(topic, group, message.id)
                handled += 1
            except Exception:  # noqa: BLE001
                continue  # 不 ack，等待 reclaim
        return handled


def _handle(handler: Callable[[Message], None], message: Message) -> None:
    """消费一条消息：接上生产者的链路（头缺失或不合法时新建 trace）。"""
    headers = message.headers or {}
    with continue_trace(headers.get("traceparent"), headers.get("X-Trace-Id"), topic=message.topic, message_id=message.id):
        handler(message)


def outbox_to_broker(broker: Broker) -> Callable[..., None]:
    """把发件箱事件投递到消息队列的 handler。"""

    def handler(topic: str, payload: dict[str, Any], event: Any = None) -> None:
        key = getattr(event, "event_id", None) or payload.get("event_id")
        enriched = {**payload, "event_id": key, "topic": topic, "published_at": time.time()}
        broker.publish(topic, enriched, key=key, headers=outgoing_headers())

    return handler
