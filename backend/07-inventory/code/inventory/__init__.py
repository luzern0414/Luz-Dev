"""inventory：库存与超卖防护（条件 UPDATE 扣减、按业务引用去重的回补、下单预占 → 确认 / 释放、库存流水）。

其他模块只从这里导入：
- 模型：Sku（skus，唯一定义在 inventory.models）、StockReservation、StockLedger、STATUS_ON_SALE / STATUS_OFF_SALE
- 扣减 / 回补：deduct(db, sku_id, qty, ref=None) -> bool、restore(db, sku_id, qty, ref=None) -> bool、
  restocked(db, ref, sku_id) -> bool
- 预占：reserve(db, ref, items, ttl_seconds, now=None)、confirm(db, ref)、release(db, ref, allow_confirmed=False)、
  expired_refs(db, now=None, limit=200)、ledger_balance(db, sku_id)（也可 `from inventory import reservation`）
本模块函数不 commit，由调用方与业务写入放在同一个事务中。
"""
from __future__ import annotations

from .models import CONFIRMED, RELEASED, RESERVED, STATUS_OFF_SALE, STATUS_ON_SALE, Sku, StockLedger, StockReservation
from .stock import deduct, restocked, restore
from . import reservation  # noqa: E402
from .reservation import RESERVATION_FLOW, confirm, expired_refs, ledger_balance, release, reserve  # noqa: E402

__all__ = [
    "CONFIRMED", "RELEASED", "RESERVED", "STATUS_OFF_SALE", "STATUS_ON_SALE", "Sku", "StockLedger", "StockReservation",
    "deduct", "restocked", "restore", "reservation",
    "RESERVATION_FLOW", "confirm", "expired_refs", "ledger_balance", "release", "reserve",
]
