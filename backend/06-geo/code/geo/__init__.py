"""地图模块：距离计算、配送范围校验、地理编码缓存与降级。

- 距离用 Haversine 球面公式，用于配送范围快速判断；精确路径距离由地图服务返回后再校验。
- 地理编码调用外部服务，结果按地址文本缓存，失败时抛 UPSTREAM_FAILED，由前端降级为“手动选点”。
- 外部客户端通过参数注入，便于测试；真实客户端见 backend/map-location/MODULE.md §3。
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable

from luzback import AppError, ErrorCode

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def check_delivery_range(store: tuple[float, float], address: tuple[float, float], max_km: float) -> float:
    """返回实际直线距离（米）。超出范围抛 OUT_OF_RANGE（4222，SPEC §4），并把距离放入 data 供前端提示。"""
    distance = haversine_m(store[0], store[1], address[0], address[1])
    if distance > max_km * 1000:
        raise AppError(
            ErrorCode.OUT_OF_RANGE,
            f"address out of delivery range ({max_km} km)",
            data={"distance_m": round(distance), "max_km": max_km},
        )
    return distance


class GeocodeCache:
    """地址文本 -> 经纬度 的带 TTL 缓存。生产可替换为 Redis。"""

    def __init__(self, client: Callable[[str], tuple[float, float]], ttl_seconds: int = 7 * 86400, clock=time.time):
        self._client = client
        self._ttl = ttl_seconds
        self._clock = clock
        self._store: dict[str, tuple[float, tuple[float, float]]] = {}
        self.upstream_calls = 0

    def geocode(self, address: str) -> tuple[float, float]:
        key = address.strip()
        if not key:
            raise AppError(ErrorCode.BAD_REQUEST, "address required")
        hit = self._store.get(key)
        if hit and hit[0] > self._clock():
            return hit[1]
        self.upstream_calls += 1
        try:
            point = self._client(key)
        except Exception as exc:
            raise AppError(ErrorCode.UPSTREAM_FAILED, "geocode service unavailable") from exc
        self._store[key] = (self._clock() + self._ttl, point)
        return point
