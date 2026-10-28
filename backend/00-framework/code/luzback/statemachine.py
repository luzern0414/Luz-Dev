"""通用状态机：声明合法流转，并用一条带条件的 UPDATE 执行。

订单、售后、预约、退款、券等所有“状态字段”都按同一方式推进：

    UPDATE t SET status = :to, ... WHERE id = :id AND status = :from

rowcount == 1 才算流转成功；为 0 说明状态已被并发请求改变或流转不合法，返回稳定错误，绝不覆盖较新的状态。
本模块不 commit，由调用方把流转与业务写入、发件箱事件放在同一事务里。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from .errors import AppError, ErrorCode


@dataclass(frozen=True)
class StateMachine:
    """name 用于错误信息；transitions 形如 {"pending": {"paid", "cancelled"}}；terminal 为终态集合。"""

    name: str
    transitions: dict[str, set[str]]
    terminal: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for source in self.terminal:
            if self.transitions.get(source):
                raise ValueError(f"{self.name}: terminal state {source} must not have outgoing transitions")

    @property
    def states(self) -> set[str]:
        result = set(self.transitions) | set(self.terminal)
        for targets in self.transitions.values():
            result |= targets
        return result

    def can(self, source: str, target: str) -> bool:
        return target in self.transitions.get(source, set())

    def sources_of(self, target: str) -> set[str]:
        return {source for source, targets in self.transitions.items() if target in targets}

    def check(self, source: str, target: str) -> None:
        if not self.can(source, target):
            raise AppError(ErrorCode.CONFLICT, f"{self.name}: illegal transition {source} -> {target}",
                           data={"from": source, "to": target})

    def advance(
        self, db: Session, model: Any, row_id: Any, source: str, target: str, *,
        status_column: str = "status", extra_where: tuple = (), **values: Any,
    ) -> None:
        """把 row_id 从 source 推进到 target；并发下失败抛 CONFLICT。values 为同时写入的字段。"""
        self.check(source, target)
        column = getattr(model, status_column)
        result = db.execute(
            update(model)
            .where(model.id == row_id, column == source, *extra_where)
            .values(**{status_column: target}, **values)
        )
        if result.rowcount != 1:
            raise AppError(ErrorCode.CONFLICT, f"{self.name}: status changed concurrently or row missing",
                           data={"id": row_id, "expected": source, "to": target})
