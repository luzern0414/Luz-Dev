"""导出标准：与列表接口用同一个 ListSpec 筛选，按行流式生成 CSV。

- 筛选条件与列表一致：导出的就是用户在列表里看到的数据。
- 行数上限：同步导出不超过 `sync_limit`（默认 5000 行），超过时由调用方改为异步导出任务（24-report）。
- 单次导出不超过 `hard_limit`（默认 20 万行），超过返回 4225，要求缩小范围。
- 公式注入：以 = + - @ 制表符或回车开头的文本前加单引号（OWASP CSV Injection）。
- 文件以 UTF-8 BOM 开头，Excel 直接打开不乱码；时间按门店时区输出。
- 敏感字段（手机号、证件号）由 serialize 负责脱敏；导出操作写审计由调用方完成。
"""
from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from .errors import AppError, ErrorCode
from .listing import ListSpec

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")  # 含全角形式（OWASP CSV Injection）


def safe_cell(value: Any, tz: str = "Asia/Shanghai") -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, datetime):
        return value.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def cents_to_yuan(cents: int | None) -> str:
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def count_rows(db: Session, spec: ListSpec, stmt: Select, params: Mapping[str, Any]) -> int:
    filtered = spec.filter(stmt, params)
    return db.execute(select(func.count()).select_from(filtered.order_by(None).subquery())).scalar_one()


def export_csv(db: Session, spec: ListSpec, stmt: Select, params: Mapping[str, Any],
               headers: Sequence[str], serialize: Callable[[Any], Sequence[Any]],
               hard_limit: int = 200_000, batch: int = 1000) -> Iterator[bytes]:
    """返回按块生成的 CSV 字节流（可直接交给 StreamingResponse）。"""
    total = count_rows(db, spec, stmt, params)
    if total > hard_limit:
        raise AppError(ErrorCode.LIMIT_REACHED, f"导出超过 {hard_limit} 行，请缩小筛选范围", {"total": total})
    ordered = spec.order(spec.filter(stmt, params), spec.sort_keys(params.get("sort")))

    def generate() -> Iterator[bytes]:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        buffer.write("﻿")
        writer.writerow([safe_cell(h, spec.tz) for h in headers])
        for partition in db.execute(ordered.execution_options(yield_per=batch)).partitions(batch):
            for row in partition:
                item = row[0] if len(row) == 1 else row
                writer.writerow([safe_cell(v, spec.tz) for v in serialize(item)])
            yield buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate(0)
        rest = buffer.getvalue()
        if rest:
            yield rest.encode("utf-8")

    return generate()
