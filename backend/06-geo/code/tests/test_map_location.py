"""地图测试：距离公式、配送范围、地理编码缓存与降级。"""
import pytest

from luzback import AppError, ErrorCode
from geo import GeocodeCache, check_delivery_range, haversine_m

# 上海人民广场与徐家汇直线距离约 5.9 公里
PEOPLE_SQUARE = (31.2304, 121.4737)
XUJIAHUI = (31.1880, 121.4370)


def test_haversine_matches_known_distance():
    d = haversine_m(*PEOPLE_SQUARE, *XUJIAHUI)
    assert 5_500 < d < 6_200


def test_within_range_returns_distance():
    d = check_delivery_range(PEOPLE_SQUARE, XUJIAHUI, max_km=10)
    assert d > 0


def test_out_of_range_reports_distance():
    with pytest.raises(AppError) as exc:
        check_delivery_range(PEOPLE_SQUARE, XUJIAHUI, max_km=3)
    assert exc.value.code == ErrorCode.OUT_OF_RANGE and exc.value.status == 422  # 4222
    assert exc.value.data["max_km"] == 3 and exc.value.data["distance_m"] > 3000


def test_geocode_uses_cache():
    calls = []

    def client(address):
        calls.append(address)
        return (31.23, 121.47)

    cache = GeocodeCache(client)
    assert cache.geocode("南京西路 1788 号") == (31.23, 121.47)
    cache.geocode("南京西路 1788 号")
    assert len(calls) == 1


def test_geocode_failure_is_upstream_error():
    def broken(address):
        raise TimeoutError("map api timeout")

    with pytest.raises(AppError) as exc:
        GeocodeCache(broken).geocode("某路 1 号")
    assert exc.value.code == ErrorCode.UPSTREAM_FAILED


def test_geocode_cache_expires():
    now = [1000.0]
    calls = []
    cache = GeocodeCache(lambda a: calls.append(a) or (1.0, 2.0), ttl_seconds=60, clock=lambda: now[0])
    cache.geocode("A")
    now[0] += 61
    cache.geocode("A")
    assert len(calls) == 2
