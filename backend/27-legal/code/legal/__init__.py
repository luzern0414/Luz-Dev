"""协议与同意：版本化文档、主动同意记录、变更重新同意、撤回（references/legal-documents.md）。

- 发布新版本只新增记录；已发布文本不可修改（内容哈希固定）。
- 同意记录只追加：用户、文档、版本、文本哈希、时间、渠道；提交时校验为当前生效版本与哈希，防止对旧文本“同意”。
- 用户是否还需同意 = 必需文档的当前版本中，没有该用户有效（未撤回）同意记录的那些。
- 撤回只标记 withdrawn_at，调用方随后停止相应处理并删除数据（如人脸）。
- 购买条款：purchase_terms_checker 接到 08-order 的 set_terms_checker（导入时自动装配），下单即记一次同意。
本模块不 commit。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from luzback import AppError, Base, ErrorCode, write_audit

REQUIRED_KINDS = ("terms", "privacy")  # 所有用户都需同意；face_consent、prepaid 等在对应功能入口单独要求
KINDS = {"terms", "privacy", "face_consent", "prepaid", "refund", "activity", "purchase"}  # purchase = 购买条款（下单时同意）


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class LegalDocument(Base):
    __tablename__ = "legal_documents"
    __table_args__ = (UniqueConstraint("kind", "version", name="uq_legal_kind_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    version: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(120))
    content: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | published | retired
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserConsent(Base):
    __tablename__ = "user_consents"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    document_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(24))
    version: Mapped[str] = mapped_column(String(24))
    content_sha256: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(24))  # miniprogram | admin_on_behalf | web
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def create_draft(db: Session, kind: str, version: str, title: str, content: str) -> LegalDocument:
    if kind not in KINDS or not version.strip() or not content.strip():
        raise AppError(ErrorCode.BAD_REQUEST, "invalid legal document")
    document = LegalDocument(kind=kind, version=version, title=title, content=content, content_sha256=content_hash(content))
    db.add(document)
    db.flush()
    return document


def publish(db: Session, document_id: int, actor_id: int, effective_at: datetime) -> LegalDocument:
    """发布草稿：同类文档的旧版本退役。写审计（权限 legal:publish 由接口层校验）。"""
    document = db.get(LegalDocument, document_id)
    if document is None or document.status != "draft":
        raise AppError(ErrorCode.CONFLICT, "only drafts can be published")
    if document.content_sha256 != content_hash(document.content):
        raise AppError(ErrorCode.CONFLICT, "draft content changed without updating its hash")
    db.execute(update(LegalDocument).where(LegalDocument.kind == document.kind, LegalDocument.status == "published")
               .values(status="retired"))
    db.execute(update(LegalDocument).where(LegalDocument.id == document.id, LegalDocument.status == "draft")
               .values(status="published", effective_at=effective_at))
    write_audit(db, "admin", actor_id, "legal.publish", "legal_document", document.id,
                after={"kind": document.kind, "version": document.version, "content_sha256": document.content_sha256})
    db.refresh(document)
    return document


def current(db: Session, kind: str, now: datetime) -> LegalDocument | None:
    rows = db.execute(select(LegalDocument).where(LegalDocument.kind == kind, LegalDocument.status == "published")).scalars().all()
    rows = [row for row in rows if row.effective_at is not None and _aware(row.effective_at) <= now]
    return max(rows, key=lambda row: _aware(row.effective_at), default=None)


def pending(db: Session, user_id: int, now: datetime, kinds: tuple[str, ...] = REQUIRED_KINDS) -> list[LegalDocument]:
    """用户尚未同意（或已撤回）的当前版本文档。"""
    result = []
    for kind in kinds:
        document = current(db, kind, now)
        if document is None:
            continue
        agreed = db.execute(select(UserConsent.id).where(
            UserConsent.user_id == user_id, UserConsent.document_id == document.id, UserConsent.withdrawn_at.is_(None))).first()
        if agreed is None:
            result.append(document)
    return result


def accept(db: Session, user_id: int, document_id: int, version: str, sha256: str, channel: str, now: datetime) -> UserConsent:
    """记录主动同意。只接受当前生效版本，且客户端展示的文本哈希必须一致。重复提交返回已有记录。"""
    document = db.get(LegalDocument, document_id)
    if document is None or document.status != "published":
        raise AppError(ErrorCode.NOT_FOUND, "document not found")
    live = current(db, document.kind, now)
    if live is None or live.id != document.id or document.version != version or document.content_sha256 != sha256:
        raise AppError(ErrorCode.CONFLICT, "document version changed, reload and confirm again",
                       data={"kind": document.kind, "current_version": live.version if live else None})
    existing = db.execute(select(UserConsent).where(
        UserConsent.user_id == user_id, UserConsent.document_id == document.id, UserConsent.withdrawn_at.is_(None))).scalar_one_or_none()
    if existing is not None:
        return existing
    consent = UserConsent(user_id=user_id, document_id=document.id, kind=document.kind, version=document.version,
                          content_sha256=document.content_sha256, channel=channel, accepted_at=now)
    db.add(consent)
    db.flush()
    return consent


def withdraw(db: Session, user_id: int, kind: str, now: datetime) -> int:
    """撤回某类文档的同意，返回撤回条数。调用方随后停止处理并删除相应数据。"""
    result = db.execute(update(UserConsent).where(UserConsent.user_id == user_id, UserConsent.kind == kind,
                                                  UserConsent.withdrawn_at.is_(None)).values(withdrawn_at=now))
    return result.rowcount


# ---------------------------------------------------------------- 08-order 购买条款


def purchase_terms_checker(db: Session, user_id: int, accepted: bool, client_version: str | None,
                           now: datetime | None = None) -> str | None:
    """08-order `set_terms_checker` 的实现：fn(db, user_id, accepted, client_version) -> 生效版本号。

    - 有已发布且已生效的 purchase 文档：必须 accepted（否则 4220 terms_required）；客户端带的版本与当前版本不同 → 4220
      terms_outdated（返回当前版本、文档 id 与哈希，前端重新展示）；通过后记一条同意记录（channel=order，同版本不重复），
      返回当前版本号写进订单快照。
    - 没有发布 purchase 文档：回落到 08-order 的配置检查（ORDER_TERMS_REQUIRED / ORDER_TERMS_VERSION）。
    """
    now = now or datetime.now(timezone.utc)
    document = current(db, "purchase", now)
    if document is None:
        try:
            from order.ports import config_terms_checker
        except ImportError:  # pragma: no cover - 未装配订单模块
            return None
        return config_terms_checker(db, user_id, accepted, client_version)
    info = {"version": document.version, "document_id": document.id, "sha256": document.content_sha256}
    if not accepted:
        raise AppError(ErrorCode.BUSINESS_RULE, "purchase terms must be accepted", data={"reason": "terms_required", **info})
    if client_version and client_version != document.version:
        raise AppError(ErrorCode.BUSINESS_RULE, "purchase terms updated, please review again",
                       data={"reason": "terms_outdated", **info})
    accept(db, user_id, document.id, document.version, document.content_sha256, "order", now)
    return document.version


def install_purchase_terms_checker() -> bool:
    """把 purchase_terms_checker 装到 08-order；订单模块未装配时返回 False。"""
    try:
        import order
    except ImportError:
        return False
    order.set_terms_checker(purchase_terms_checker)
    return True


install_purchase_terms_checker()
