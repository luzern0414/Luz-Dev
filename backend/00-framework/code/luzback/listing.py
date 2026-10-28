"""列表查询标准（SPEC §3.4）：筛选、排序、分页、关键词搜索，所有模块的列表接口共用。

一个列表接口只做三件事：声明 `ListSpec`（哪些字段能筛、能排、能搜），把查询参数交给它，返回它给出的结果。

查询参数约定（顾客端与后台一致）：

| 写法 | 含义 |
|---|---|
| `status=paid` / `status=paid,refunded` | 等于 / 属于（多值逗号分隔，最多 50 个） |
| `amount_cents_min=100&amount_cents_max=500` | 数值区间，两端都包含 |
| `created_at_from=2026-10-01&created_at_to=2026-10-31` | 时间区间：日期按门店时区整天计算，`to` 当天包含；带时刻时 `from` 包含、`to` 不包含 |
| `keyword=张` | 在声明的列里模糊匹配（转义 `%` `_`），1–50 个字符，前后空格忽略 |
| `sort=-created_at,amount_cents` | 白名单字段，`-` 降序，最多 3 个；自动追加 `id` 作为最后的排序键，保证顺序稳定 |
| `page=1&page_size=20` | 后台页码分页，页大小上限 100，`page × page_size` 不超过 10000（更深的翻页要求先缩小筛选范围） |
| `cursor=…&limit=10` | 顾客端游标分页，上限 20；游标签名防篡改，绑定排序与筛选条件，条件变了或超过 3 天旧游标作废 |

未声明的参数一律返回 4001（参数错误），避免拼错的筛选条件被静默忽略而返回全部数据。
空值（`status=`）视为未传。NULL 值在升序和降序中都排在最后。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time as _time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Select, and_, false, func, or_, select
from sqlalchemy.orm import Session

from .errors import AppError, ErrorCode

RESERVED = {"page", "page_size", "cursor", "limit", "sort", "keyword"}
MAX_IN_VALUES = 50
MAX_SORT_KEYS = 3
KEYWORD_MAX = 50
CURSOR_TTL_SECONDS = 3 * 24 * 3600  # 游标 3 天后失效，避免长期保存的游标在数据变化后继续使用

KINDS = ("str", "int", "bool", "date", "datetime")
OPS = ("eq", "in", "range", "null")


def bad(message: str) -> AppError:
    return AppError(ErrorCode.BAD_REQUEST, message)


@dataclass(frozen=True)
class Field:
    """一个可筛选 / 可排序的字段。

    column: ORM 列或任意 SQL 表达式（例如关联表的列、`func.coalesce(...)`）。
    kind: 值类型，决定参数解析与游标序列化。
    ops: 允许的筛选方式：eq（等于）、in（多值）、range（区间）、null（`field_null=true|false`）。
    choices: 枚举字段的合法值；传入其他值返回 4001。
    sortable: 是否允许出现在 sort 参数里。
    """

    column: Any
    kind: str = "str"
    ops: tuple[str, ...] = ("eq",)
    choices: frozenset[str] | None = None
    sortable: bool = False

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind {self.kind}")
        if not set(self.ops) <= set(OPS):
            raise ValueError(f"unknown ops {self.ops}")


@dataclass
class ListSpec:
    """列表接口的声明。

    fields: 参数名 → Field。参数名就是接口文档里的名字，可以和列名不同。
    id_column: 最后的排序键（唯一且不为空），通常是主键。
    keyword_columns: keyword 匹配的列，任一列包含关键词即命中。
    default_sort: 未传 sort 时的排序。
    tz: 日期筛选按哪个时区计算整天（门店时区）。
    max_page_size / max_window / max_limit: 分页上限。
    max_range_days: 时间区间最长跨度（例如导出与报表限制 31 天）；None 表示不限。
    """

    fields: Mapping[str, Field]
    id_column: Any
    keyword_columns: tuple[Any, ...] = ()
    default_sort: str = "-created_at"
    tz: str = "Asia/Shanghai"
    max_page_size: int = 100
    default_page_size: int = 20
    max_window: int = 10_000
    default_limit: int = 10
    max_limit: int = 20
    max_range_days: int | None = None
    extra_params: frozenset[str] = field(default_factory=frozenset)

    # ------------------------------------------------------------ filters

    def filter(self, stmt: Select, params: Mapping[str, Any]) -> Select:
        """按查询参数追加 WHERE 条件。params 可以是 request.query_params 或普通 dict。"""
        names = {key for key in params if key not in RESERVED and key not in self.extra_params}
        allowed = set()
        for name, spec in self.fields.items():
            if "eq" in spec.ops or "in" in spec.ops:
                allowed.add(name)
            if "range" in spec.ops:
                allowed |= {f"{name}_min", f"{name}_max"} if spec.kind in ("int", "str") else {f"{name}_from", f"{name}_to"}
            if "null" in spec.ops:
                allowed.add(f"{name}_null")
        unknown = sorted(names - allowed)
        if unknown:
            raise bad(f"unknown query parameter: {', '.join(unknown)}")

        conditions = []
        for name, spec in self.fields.items():
            raw = _value(params, name)
            if raw is not None:
                values = [v.strip() for v in raw.split(",")] if "in" in spec.ops else [raw.strip()]
                values = [v for v in values if v != ""]
                if len(values) > MAX_IN_VALUES:
                    raise bad(f"{name}: at most {MAX_IN_VALUES} values")
                parsed = [self._parse(name, spec, v) for v in values]
                if len(parsed) == 1:
                    conditions.append(spec.column == parsed[0])
                elif parsed:
                    conditions.append(spec.column.in_(parsed))
            if "range" in spec.ops:
                conditions += self._range(name, spec, params)
            if "null" in spec.ops:
                flag = _value(params, f"{name}_null")
                if flag is not None:
                    conditions.append(spec.column.is_(None) if _parse_bool(f"{name}_null", flag) else spec.column.is_not(None))

        keyword = _value(params, "keyword")
        if keyword is not None:
            keyword = keyword.strip()
            if keyword:
                if not self.keyword_columns:
                    raise bad("keyword search is not supported here")
                if len(keyword) > KEYWORD_MAX:
                    raise bad(f"keyword: at most {KEYWORD_MAX} characters")
                pattern = f"%{escape_like(keyword)}%"
                conditions.append(or_(*(col.ilike(pattern, escape="\\") for col in self.keyword_columns)))
        return stmt.where(and_(*conditions)) if conditions else stmt

    def _range(self, name: str, spec: Field, params: Mapping[str, Any]) -> list[Any]:
        numeric = spec.kind in ("int", "str")
        low_key, high_key = (f"{name}_min", f"{name}_max") if numeric else (f"{name}_from", f"{name}_to")
        low_raw, high_raw = _value(params, low_key), _value(params, high_key)
        low_raw = low_raw.strip() if low_raw else None
        high_raw = high_raw.strip() if high_raw else None
        if not low_raw and not high_raw:
            return []
        if numeric:
            low = self._parse(low_key, spec, low_raw) if low_raw else None
            high = self._parse(high_key, spec, high_raw) if high_raw else None
            if low is not None and high is not None and low > high:
                raise bad(f"{low_key} must not exceed {high_key}")
            out = []
            if low is not None:
                out.append(spec.column >= low)
            if high is not None:
                out.append(spec.column <= high)
            return out
        low = self._bound(low_key, spec, low_raw, upper=False) if low_raw else None
        high = self._bound(high_key, spec, high_raw, upper=True) if high_raw else None
        if low is not None and high is not None:
            if low >= high:
                raise bad(f"{low_key} must be earlier than {high_key}")
            if self.max_range_days is not None and high - low > timedelta(days=self.max_range_days):
                raise bad(f"date range longer than {self.max_range_days} days")
        out = []
        if low is not None:
            out.append(spec.column >= low)
        if high is not None:
            out.append(spec.column < high)
        return out

    def _bound(self, key: str, spec: Field, raw: str, upper: bool) -> Any:
        """返回半开区间 [from, to) 的端点；日期型字段返回 date，时间型字段返回带时区的 datetime。"""
        tz = ZoneInfo(self.tz)
        if len(raw) == 10:  # 只有日期：按门店时区的整天
            try:
                day = date.fromisoformat(raw)
            except ValueError as exc:
                raise bad(f"{key}: expected YYYY-MM-DD") from exc
            if upper:
                day += timedelta(days=1)
            if spec.kind == "date":
                return day
            return datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise bad(f"{key}: expected ISO 8601 date or datetime") from exc
        if spec.kind == "date":
            raise bad(f"{key}: expected YYYY-MM-DD")
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=tz)
        return moment.astimezone(timezone.utc)

    def _parse(self, key: str, spec: Field, raw: str) -> Any:
        if spec.choices is not None and raw not in spec.choices:
            raise bad(f"{key}: must be one of {', '.join(sorted(spec.choices))}")
        if spec.kind == "int":
            try:
                return int(raw)
            except ValueError as exc:
                raise bad(f"{key}: expected integer") from exc
        if spec.kind == "bool":
            return _parse_bool(key, raw)
        if spec.kind == "date":
            try:
                return date.fromisoformat(raw)
            except ValueError as exc:
                raise bad(f"{key}: expected YYYY-MM-DD") from exc
        if spec.kind == "datetime":
            return self._bound(key, spec, raw, upper=False)
        if len(raw) > 200:
            raise bad(f"{key}: value too long")
        return raw

    # ------------------------------------------------------------ sorting

    def sort_keys(self, sort: str | None) -> list[tuple[str, Any, str, bool]]:
        """解析 sort 参数，返回 [(参数名, 列, 类型, 是否降序)]，最后一项总是 id。"""
        keys: list[tuple[str, Any, str, bool]] = []
        seen: set[str] = set()
        parts = [p.strip() for p in (sort or self.default_sort).split(",") if p.strip()]
        for index, raw in enumerate(parts):
            desc = raw.startswith("-")
            name = raw[1:] if desc else raw
            if name == "id":
                if index != len(parts) - 1:
                    raise bad("id must be the last sort field")
                keys.append(("id", self.id_column, "int", desc))
                seen.add(name)
                break
            spec = self.fields.get(name)
            if spec is None or not spec.sortable:
                raise bad(f"sort field not allowed: {name}")
            if name in seen:
                raise bad(f"sort field repeated: {name}")
            seen.add(name)
            keys.append((name, spec.column, spec.kind, desc))
        if len([k for k in keys if k[0] != "id"]) > MAX_SORT_KEYS:
            raise bad(f"at most {MAX_SORT_KEYS} sort fields")
        if "id" not in seen:
            keys.append(("id", self.id_column, "int", keys[-1][3] if keys else True))
        return keys

    @staticmethod
    def order(stmt: Select, keys: list[tuple[str, Any, str, bool]]) -> Select:
        clauses = []
        for _name, column, _kind, desc in keys:
            expr = column.desc() if desc else column.asc()
            clauses.append(expr.nulls_last())
        return stmt.order_by(*clauses)

    # ------------------------------------------------------------ pagination

    def page(self, db: Session, stmt: Select, params: Mapping[str, Any],
             serialize: Callable[[Any], Any] = lambda row: row) -> dict[str, Any]:
        """后台页码分页：{items, page, page_size, total}。stmt 只写 select 与固定条件，筛选和排序由这里追加。"""
        page = _int(params, "page", 1)
        size = _int(params, "page_size", self.default_page_size)
        if page < 1 or size < 1:
            raise bad("page and page_size must be >= 1")
        size = min(size, self.max_page_size)
        if page * size > self.max_window:
            raise bad(f"too deep: narrow the filters (page × page_size ≤ {self.max_window})")
        filtered = self.filter(stmt, params)
        total = db.execute(select(func.count()).select_from(filtered.order_by(None).subquery())).scalar_one()
        rows = db.execute(self.order(filtered, self.sort_keys(_value(params, "sort"))).limit(size).offset((page - 1) * size))
        items = [serialize(r) for r in _rows(rows)]
        return {"items": items, "page": page, "page_size": size, "total": total}

    def cursor(self, db: Session, stmt: Select, params: Mapping[str, Any], secret: str,
               serialize: Callable[[Any], Any] = lambda row: row,
               row_values: Callable[[Any], Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """顾客端游标分页（键集分页）：{items, next_cursor, has_more}。

        row_values(row) 返回排序字段的值（参数名 → 值）；默认按参数名读取行对象的属性，
        排序键是关联表或表达式时必须提供。
        """
        limit = _int(params, "limit", self.default_limit)
        if limit < 1:
            raise bad("limit must be >= 1")
        limit = min(limit, self.max_limit)
        keys = self.sort_keys(_value(params, "sort"))
        binding = self._binding(params, keys)
        filtered = self.filter(stmt, params)
        token = _value(params, "cursor")
        if token:
            values = self._decode(token, secret, binding, keys)
            filtered = filtered.where(_after(keys, values))
        rows = _rows(db.execute(self.order(filtered, keys).limit(limit + 1)))
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            raw = row_values(last) if row_values else {name: getattr(last, name) for name, *_ in keys}
            next_cursor = self._encode([_dump(raw[name], kind) for name, _c, kind, _d in keys], secret, binding)
        return {"items": [serialize(r) for r in rows], "next_cursor": next_cursor, "has_more": has_more}

    def _binding(self, params: Mapping[str, Any], keys: list[tuple[str, Any, str, bool]]) -> str:
        """游标绑定排序和筛选条件：条件不同的游标不能混用。"""
        relevant = sorted((k, str(_value(params, k))) for k in params if k not in {"cursor", "limit", "page", "page_size"})
        sig = json.dumps([[(n, d) for n, _c, _k, d in keys], relevant], ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(sig.encode()).hexdigest()[:12]

    @staticmethod
    def _encode(values: list[Any], secret: str, binding: str) -> str:
        body = json.dumps({"v": values, "b": binding, "t": int(_time.time())}, separators=(",", ":")).encode()
        token = base64.urlsafe_b64encode(body).rstrip(b"=").decode()
        mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()[:16]
        return f"{token}.{mac}"

    @staticmethod
    def _decode(cursor: str, secret: str, binding: str, keys: list[tuple[str, Any, str, bool]]) -> list[Any]:
        try:
            token, mac = cursor.split(".")
            body = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            if not hmac.compare_digest(hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()[:16], mac):
                raise ValueError("signature")
            data = json.loads(body)
            if data["b"] != binding or len(data["v"]) != len(keys):
                raise ValueError("binding")
            if _time.time() - int(data.get("t", 0)) > CURSOR_TTL_SECONDS:
                raise ValueError("expired")
            return [_load(v, kind) for v, (_n, _c, kind, _d) in zip(data["v"], keys)]
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise bad("invalid cursor: start again from the first page") from exc


# ---------------------------------------------------------------- helpers


def escape_like(value: str) -> str:
    """转义 LIKE 通配符，配合 escape='\\' 使用。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _value(params: Mapping[str, Any], key: str) -> str | None:
    if key not in params:
        return None
    value = params[key]
    if isinstance(value, (list, tuple)):  # dict 形式传入多值
        value = ",".join(str(v) for v in value)
    return None if value is None else str(value)


def _int(params: Mapping[str, Any], key: str, default: int) -> int:
    raw = _value(params, key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise bad(f"{key}: expected integer") from exc


def _parse_bool(key: str, raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in ("true", "1"):
        return True
    if lowered in ("false", "0"):
        return False
    raise bad(f"{key}: expected true or false")


def _rows(result: Any) -> list[Any]:
    rows = result.all()
    if rows and len(rows[0]) == 1:  # select(Model) → 取模型对象
        return [r[0] for r in rows]
    return list(rows)


def _dump(value: Any, kind: str) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _load(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind == "datetime":
        return datetime.fromisoformat(value)
    if kind == "date":
        return date.fromisoformat(value)
    if kind == "int":
        return int(value)
    if kind == "bool":
        return bool(value)
    return str(value)


def _after(keys: list[tuple[str, Any, str, bool]], values: list[Any]) -> Any:
    """键集分页条件：排在游标之后的行。NULL 排在最后，与 order() 一致。

    对 (k1, k2, …, id) 展开为：k1 在后 OR (k1 相等 AND k2 在后) OR …
    """
    branches = []
    for i, (_name, column, _kind, desc) in enumerate(keys):
        prefix = [_equal(keys[j][1], values[j]) for j in range(i)]
        branches.append(and_(*prefix, _beyond(column, values[i], desc)))
    return or_(*branches)


def _equal(column: Any, value: Any) -> Any:
    return column.is_(None) if value is None else column == value


def _beyond(column: Any, value: Any, desc: bool) -> Any:
    if value is None:  # 已经在 NULL 段：之后没有更"大"的非空值
        return false()
    after = column < value if desc else column > value
    return or_(after, column.is_(None))


def fields_from(columns: Iterable[tuple[str, Any, str]], **common: Any) -> dict[str, Field]:
    """便捷构造：[(参数名, 列, 类型)] → {参数名: Field(**common)}。"""
    return {name: Field(column, kind, **common) for name, column, kind in columns}
