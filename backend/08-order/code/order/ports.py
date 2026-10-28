"""order 的外部依赖适配（全部可替换；默认委托对应模块的公开导出，延迟导入，模块未启用时退化）。

| 端口 | 默认 | 替换 |
|---|---|---|
| 活动 promotions | 无活动（[]） | `set_promotions_provider(fn(db, user_id, store_id, lines, now) -> list[pricing.Promotion])` |
| 优惠券 coupon | 16-coupon：price_with_coupons / available_coupons / lock / redeem / unlock / return_on_refund | `set_coupon_port(CouponPort)` |
| 下单人联系方式 | 01-identity `profile(reveal_phone=True)` → 脱敏 + 哈希 | `set_buyer_contact_provider(fn(db, user_id) -> phone or None)` |
| 收货地址 | 05-address 本人地址 → 快照 | `set_address_provider(fn(db, user_id, address_id) -> dict)` |
| 购买条款 | 客户端显式同意 + 配置版本号（ORDER_TERMS_VERSION）；装配 27-legal 时自动换成 `legal.purchase_terms_checker`（以已发布的 purchase 文档为准并记同意） | `set_terms_checker(fn(db, user_id, accepted, version) -> version)` |
| 微信支付 | 09-wechat-pay | `set_payment_gateway(PaymentGateway)` |
| 余额 | 15-recharge `consume` / `reverse` | `set_wallet_port(WalletPort)` |
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, get_settings, mask_phone
from luzback.crypto import FieldCipher

log = logging.getLogger("luz.order")

# ---------------------------------------------------------------- 活动

PromotionsProvider = Callable[[Session, int, int | None, list[Any], datetime], list[Any]]
_promotions: PromotionsProvider | None = None


def set_promotions_provider(fn: PromotionsProvider | None) -> None:
    global _promotions
    _promotions = fn


def promotions(db: Session, user_id: int, store_id: int | None, lines: list[Any], now: datetime) -> list[Any]:
    return list(_promotions(db, user_id, store_id, lines, now)) if _promotions else []


# ---------------------------------------------------------------- 优惠券


class CouponPort:
    """16-coupon 的薄封装；没有启用券模块时 enabled=False，订单只做活动计价。"""

    @property
    def enabled(self) -> bool:
        try:
            import coupon  # noqa: F401
        except ImportError:
            return False
        return True

    def cart_line(self, line: Any) -> Any:
        from coupon import CartLine
        return CartLine(sku_id=int(line.sku_id or line.product_id), product_id=int(line.product_id), qty=line.qty,
                        unit_price_cents=line.unit_price_cents, category_id=line.category_id, product_kind=line.kind)

    def price(self, db: Session, user_id: int, coupon_ids: list[int], lines: list[Any], promos: list[Any],
              now: datetime, order_id: int | None = None) -> tuple[Any, list[int], list[dict]]:
        """返回 (pricing.Quote（活动 + 券）, 每行券分摊, 券明细)。任何一张券不可用 → 4220（带原因）。"""
        import coupon
        result = coupon.price_with_coupons(db, user_id, coupon_ids, [self.cart_line(x) for x in lines], promos,
                                           now=now, order_id=order_id)
        return result.evaluation.quote, list(result.line_coupon_cents), list(result.coupons)

    def available(self, db: Session, user_id: int, lines: list[Any], promos: list[Any], now: datetime) -> dict:
        import coupon
        return coupon.available_coupons(db, user_id, [self.cart_line(x) for x in lines], promos, now=now)

    def lock(self, db, user_id, order_id, coupon_ids, lines, promos, now) -> None:
        import coupon
        coupon.lock(db, user_id, order_id, coupon_ids, [self.cart_line(x) for x in lines], promos, now=now)

    def redeem(self, db: Session, order_id: int, now: datetime) -> None:
        import coupon
        coupon.redeem(db, order_id, now=now)

    def unlock(self, db: Session, order_id: int, now: datetime) -> None:
        import coupon
        coupon.unlock(db, order_id, now=now)

    def return_on_refund(self, db: Session, order_id: int, full_refund: bool, now: datetime) -> dict:
        import coupon
        return coupon.return_on_refund(db, order_id, full_refund, now=now)


_coupon = CouponPort()


def set_coupon_port(port: CouponPort | None) -> None:
    global _coupon
    _coupon = port or CouponPort()


def coupon_port() -> CouponPort:
    return _coupon


# ---------------------------------------------------------------- 下单人 / 地址 / 条款

BuyerContactProvider = Callable[[Session, int], str | None]
_buyer_contact: BuyerContactProvider | None = None


def set_buyer_contact_provider(fn: BuyerContactProvider | None) -> None:
    global _buyer_contact
    _buyer_contact = fn


def _identity_phone(db: Session, user_id: int) -> str | None:
    try:
        import identity
    except ImportError:
        return None
    try:
        return identity.profile(db, user_id, reveal_phone=True).get("phone")
    except AppError:
        return None


def buyer_contact(db: Session, user_id: int) -> tuple[str | None, str | None]:
    """返回 (脱敏手机号, 查询哈希)；订单只存这两项，不存明文。"""
    phone = (_buyer_contact or _identity_phone)(db, user_id)
    if not phone or "*" in phone:
        return (phone or None), None
    return mask_phone(phone), phone_hash(phone)


def _cipher() -> FieldCipher:
    return FieldCipher(get_settings().field_encryption_key)


def phone_hash(phone: str) -> str:
    return _cipher().lookup_hash(phone.strip()) or ""


def encrypt_json(text: str | None) -> str | None:
    """地址快照含收件人手机号：整体加密存储（AES-GCM，LUZ_FIELD_KEY），读取时解密并按场景脱敏。"""
    return _cipher().encrypt(text) if text else None


def decrypt_json(token: str | None) -> str | None:
    return _cipher().decrypt(token) if token else None


AddressProvider = Callable[[Session, int, int], dict[str, Any]]
_address: AddressProvider | None = None


def set_address_provider(fn: AddressProvider | None) -> None:
    global _address
    _address = fn


def _address_default(db: Session, user_id: int, address_id: int) -> dict[str, Any]:
    try:
        import address
    except ImportError as exc:
        raise AppError(ErrorCode.BAD_REQUEST, "address module disabled") from exc
    row = db.get(address.Address, address_id)
    if row is None or getattr(row, "user_id", None) != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "address not found")
    fields = ("contact", "phone", "province", "city", "district", "detail", "house_no", "lat", "lng")
    return {k: getattr(row, k) for k in fields if hasattr(row, k)}


def address_snapshot(db: Session, user_id: int, address_id: int) -> dict[str, Any]:
    return (_address or _address_default)(db, user_id, address_id)


TermsChecker = Callable[[Session, int, bool, str | None], str]
_terms: TermsChecker | None = None


def set_terms_checker(fn: TermsChecker | None) -> None:
    """fn(db, user_id, accepted, client_version) -> 生效版本号；不满足抛 4220。可接入 27-legal 的同意记录。"""
    global _terms
    _terms = fn


def check_terms(db: Session, user_id: int, accepted: bool, client_version: str | None) -> str | None:
    return (_terms or config_terms_checker)(db, user_id, accepted, client_version)


def config_terms_checker(db: Session, user_id: int, accepted: bool, client_version: str | None) -> str | None:
    """默认检查：显式同意 + 配置版本号（ORDER_TERMS_REQUIRED / ORDER_TERMS_VERSION）。27-legal 未发布购买条款时也回落到这里。"""
    from .config import get_config
    cfg = get_config()
    if not cfg.terms_required:
        return None
    if not accepted:
        raise AppError(ErrorCode.BUSINESS_RULE, "purchase terms must be accepted", data={"reason": "terms_required",
                                                                                          "version": cfg.terms_version})
    if client_version and client_version != cfg.terms_version:
        raise AppError(ErrorCode.BUSINESS_RULE, "purchase terms updated, please review again",
                       data={"reason": "terms_outdated", "version": cfg.terms_version})
    return cfg.terms_version


# ---------------------------------------------------------------- 支付


class PaymentGateway:
    """09-wechat-pay 适配。注意：这些调用会提交会话（渠道调用前结束事务），只能在业务事务之外调用。"""

    def pay(self, db: Session, user_id: int, order_id: int, now: datetime | None = None) -> dict[str, Any]:
        import wechat_pay
        return wechat_pay.pay_business(db, user_id, "order", order_id, now=now)

    def open_payment(self, db: Session, order_id: int) -> dict[str, Any] | None:
        """最近一笔支付单（dict，含 id / status）；没有返回 None。"""
        try:
            import wechat_pay
        except ImportError:
            return None
        return wechat_pay.get_business_payment(db, "order", order_id)

    def close(self, db: Session, payment_id: int, reason: str, now: datetime | None = None) -> str:
        """先查单再关单（09 close_payment）：返回 paid / closed / failed / closing（渠道暂不可用）。"""
        import wechat_pay
        return wechat_pay.close_payment(db, payment_id, reason, now=now)["status"]

    def shipping(self, db: Session, payment_id: int, item_desc: str, *, logistics_type: int,
                 tracking_no: str | None = None, express_company: str | None = None) -> None:
        import wechat_pay
        wechat_pay.request_shipping_upload(db, payment_id, item_desc, logistics_type=logistics_type,
                                           tracking_no=tracking_no, express_company=express_company)


_gateway = PaymentGateway()


def set_payment_gateway(gateway: PaymentGateway | None) -> None:
    global _gateway
    _gateway = gateway or PaymentGateway()


def payment_gateway() -> PaymentGateway:
    return _gateway


class WalletPort:
    """15-recharge 余额：按行消费（ref = order:{id}:item:{item_id}），整行退款按 ref 冲回。"""

    def consume(self, db: Session, user_id: int, ref: str, amount_cents: int, store_id: int | None) -> None:
        import wallet
        wallet.consume(db, user_id, ref, amount_cents, store_id=store_id)

    def reverse(self, db: Session, user_id: int, ref: str, amount_cents: int | None = None,
                reversal_ref: str | None = None) -> None:
        """冲回消费：amount_cents=None 冲回剩余全部；部分冲回必须给 reversal_ref（按它幂等）。"""
        import wallet
        wallet.reverse(db, user_id, ref, amount_cents=amount_cents, reversal_ref=reversal_ref)


_wallet = WalletPort()


def set_wallet_port(port: WalletPort | None) -> None:
    global _wallet
    _wallet = port or WalletPort()


def wallet_port() -> WalletPort:
    return _wallet
