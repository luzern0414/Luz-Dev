# 模块：map-location（地图与配送范围）

| 项 | 内容 |
|---|---|
| 状态 | 已实现（Haversine 距离、配送范围校验、地理编码缓存与降级），测试 5 项通过；腾讯地图客户端为设计 |
| 代码 | `06-geo/code/geo/`、测试 `06-geo/code/tests/` |
| 依赖 | framework、address（地址坐标）、order（下单时校验范围） |

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| 两点直线距离（米） | 路线规划、ETA 计算（地图服务返回，设计） |
| 门店配送范围判断 | 骑手实时位置（delivery-dispatch） |
| 地址文本 → 经纬度（地理编码）及缓存 | 地图底图渲染（小程序 map 组件或地图 SDK） |
| 地图服务失败时的降级信号 | 逆地理编码的业务用途（只做展示） |

## 2 数据模型

| 表 | 字段 | 说明 |
|---|---|---|
| `stores` | id, name, lat, lng, delivery_radius_km, open_hours, status | 门店坐标与配送半径（设计，门店模块） |
| 地理编码缓存 | 进程内存（开发）；生产为 Redis `geo:{sha1(address)}`，TTL 7 天 | 不入库，避免与地图服务条款冲突时的数据留存问题 |

## 3 外部接入（腾讯位置服务，设计）

| 能力 | 接口 | 说明 |
|---|---|---|
| 地址解析 | `https://apis.map.qq.com/ws/geocoder/v1/?address=...&key=...` | 返回 `result.location.lat/lng`；超时 3 秒 |
| 路线距离 | `https://apis.map.qq.com/ws/distance/v1/?from=...&to=...&mode=driving` | 配送范围边界模糊时使用 |
| 逆地理 | `.../ws/geocoder/v1/?location=lat,lng` | 定位后回填地址文本 |

Key 只从环境变量读取；按小程序域名白名单与服务端 IP 白名单双重限制。

## 4 关键规则

| 规则 | 说明 |
|---|---|
| 配送范围 | 默认用直线距离判断；直线距离在 `半径 × 0.8` 以内直接通过，在 `半径 × 1.2` 以外直接拒绝，中间区间调用路线距离接口 |
| 坐标系 | 统一 GCJ-02（腾讯地图）；若接入其他 SDK 需转换，禁止混用 |
| 地址质量 | 地理编码结果与用户输入的省市区不一致时，要求用户确认 |
| 降级 | 地图服务不可用：允许用户手动选点（坐标由前端地图组件取得），后端仅做范围判断 |

## 5 接口

| 方法 | 路径 | 鉴权 | agent | 说明 |
|---|---|---|---|---|
| GET | `/api/v1/app/geo/reverse?lat=&lng=` | access | yes | 逆地理，供“使用当前位置” |
| GET | `/api/v1/app/geo/geocode?address=` | access | no | 地址解析（填写页“自动回填坐标”） |
| GET | `/api/v1/app/stores/{id}/delivery-check?lat=&lng=` | access | yes | 返回 `{in_range, distance_m, max_km}` |

## 6 并发与一致性

纯计算与缓存，无数据库写入。缓存击穿：同一地址的并发请求由单飞（single-flight）合并为一次上游调用（生产用 Redis 锁或进程内 Future 合并）。

## 7 配置

| 变量 | 说明 |
|---|---|
| `TENCENT_MAP_KEY` | 地图服务密钥（密钥服务注入） |
| `GEO_CACHE_TTL_DAYS` | 默认 7 |
| `GEO_UPSTREAM_TIMEOUT_SECONDS` | 默认 3 |

## 8 错误码

| 场景 | code | HTTP |
|---|---|---|
| 地址为空 | 4001 | 400 |
| 超出配送范围 | 4220 | 422（data 含 distance_m、max_km） |
| 地图服务失败 | 5020 | 502 |

## 9 测试用例（已实现）

| 用例 | 断言 |
|---|---|
| 已知两点距离 | 约 5.9 km（人民广场—徐家汇） |
| 范围内 | 返回距离 |
| 范围外 | 4220，data 含距离 |
| 缓存命中 | 上游只调用一次 |
| 上游超时 | 5020 |
| 缓存过期 | 再次调用上游 |

## 10 可复用代码

`06-geo/code/geo/__init__.py`：`haversine_m`、`check_delivery_range`、`GeocodeCache`。

## 11 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 地图选址页 | reverse、geocode | 拖动地图中心点即时逆地理，松手后刷新地址文本 |
| 确认订单（外卖） | delivery-check | 选址后检查范围：范围外在地址行显示“超出配送范围 X 公里” |
| 地图失败 | — | 显示“地图暂不可用，请手动填写地址”，允许跳过坐标 |
