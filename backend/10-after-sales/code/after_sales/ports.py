"""after_sales 外部依赖适配（可替换）。

| 端口 | 默认 | 替换 |
|---|---|---|
| 微信退款 RefundGateway | 09-wechat-pay `create_refund`（同一 out_refund_no 重试由 09 处理）；提交前用 `wechat_pay.find_active_refund(db, business_ref)` 查已有退款防重复 | `set_refund_gateway(...)` |
| 凭证图片 evidence_checker | 不校验（只校验数量与 id 形式） | `set_evidence_checker(fn(db, user_id, media_ids) -> None)`（接入 21-media 归属校验） |
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session


class RefundGateway:
    """09-wechat-pay 适配。create 会提交会话（渠道调用不在业务事务内）。"""

    def existing(self, db: Session, business_ref: str) -> dict[str, Any] | None:
        """同一售后单仍有效的退款（未失败 / 未关闭）：崩溃重提时直接采纳，避免重复退款。"""
        import wechat_pay
        row = wechat_pay.find_active_refund(db, business_ref)
        if row is None:
            return None
        return {"refund_id": row.id, "out_refund_no": row.out_refund_no, "status": row.status}

    def create(self, db: Session, payment_id: int, amount_cents: int, reason: str, business_ref: str,
               now: datetime | None = None) -> dict[str, Any]:
        import wechat_pay
        info = wechat_pay.create_refund(db, payment_id, amount_cents, reason, business_ref, actor="after_sales", now=now)
        return {"refund_id": info.refund_id, "out_refund_no": info.out_refund_no, "status": info.status}


_gateway = RefundGateway()


def set_refund_gateway(gateway: RefundGateway | None) -> None:
    global _gateway
    _gateway = gateway or RefundGateway()


def refund_gateway() -> RefundGateway:
    return _gateway


EvidenceChecker = Callable[[Session, int, list[int]], None]
_evidence: EvidenceChecker | None = None


def set_evidence_checker(fn: EvidenceChecker | None) -> None:
    global _evidence
    _evidence = fn


def check_evidence(db: Session, user_id: int, media_ids: list[int]) -> None:
    if _evidence is not None and media_ids:
        _evidence(db, user_id, media_ids)
