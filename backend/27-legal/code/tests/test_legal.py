"""协议与同意：发布退役旧版、只能同意当前版本与哈希、变更后需重新同意、撤回、审计。"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import legal
from legal import accept, content_hash, create_draft, pending, publish, withdraw
from luzback import AppError, AuditLog, ErrorCode
from luzback.testing import memory_session

NOW = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db():
    session = memory_session(legal, "luzback.audit")
    yield session
    session.close()


def release(db, kind, version, text, when=NOW):
    doc = create_draft(db, kind, version, f"{kind} {version}", text)
    return publish(db, doc.id, actor_id=1, effective_at=when)


def test_new_user_must_accept_current_terms_and_privacy(db):
    terms, privacy = release(db, "terms", "1.0", "条款一"), release(db, "privacy", "1.0", "隐私一")
    assert {d.kind for d in pending(db, 7, NOW)} == {"terms", "privacy"}
    accept(db, 7, terms.id, "1.0", content_hash("条款一"), "miniprogram", NOW)
    again = accept(db, 7, terms.id, "1.0", content_hash("条款一"), "miniprogram", NOW)
    assert [d.kind for d in pending(db, 7, NOW)] == ["privacy"]
    assert len(db.execute(select(legal.UserConsent)).scalars().all()) == 1 and again.version == "1.0"
    assert db.execute(select(AuditLog.action)).scalars().all() == ["legal.publish", "legal.publish"]


def test_hash_or_version_mismatch_is_rejected(db):
    terms = release(db, "terms", "1.0", "条款一")
    with pytest.raises(AppError) as error:
        accept(db, 7, terms.id, "1.0", content_hash("被篡改的文本"), "miniprogram", NOW)
    assert error.value.code == ErrorCode.CONFLICT


def test_new_version_requires_reconsent_and_old_cannot_be_accepted(db):
    old = release(db, "terms", "1.0", "条款一")
    accept(db, 7, old.id, "1.0", content_hash("条款一"), "miniprogram", NOW)
    new = release(db, "terms", "2.0", "条款二", NOW + timedelta(days=1))
    later = NOW + timedelta(days=2)
    assert [d.version for d in pending(db, 7, later, ("terms",))] == ["2.0"]
    db.refresh(old)
    assert old.status == "retired"
    with pytest.raises(AppError):
        accept(db, 8, old.id, "1.0", content_hash("条款一"), "miniprogram", later)
    accept(db, 7, new.id, "2.0", content_hash("条款二"), "miniprogram", later)
    assert pending(db, 7, later, ("terms",)) == []


def test_withdraw_requires_consent_again(db):
    face = release(db, "face_consent", "1.0", "人脸同意")
    accept(db, 7, face.id, "1.0", content_hash("人脸同意"), "miniprogram", NOW)
    assert pending(db, 7, NOW, ("face_consent",)) == []
    assert withdraw(db, 7, "face_consent", NOW) == 1
    assert [d.kind for d in pending(db, 7, NOW, ("face_consent",))] == ["face_consent"]


def test_published_document_cannot_be_republished(db):
    doc = release(db, "privacy", "1.0", "隐私一")
    with pytest.raises(AppError):
        publish(db, doc.id, actor_id=1, effective_at=NOW)


# ---------------------------------------------------------------- 购买条款 → 08-order

def test_purchase_terms_checker_uses_published_document(db):
    from legal import purchase_terms_checker

    # 未发布 purchase 文档：回落到订单配置（默认要求同意，版本 purchase-v1）
    with pytest.raises(AppError) as error:
        purchase_terms_checker(db, 7, False, None, now=NOW)
    assert error.value.data["reason"] == "terms_required"
    assert purchase_terms_checker(db, 7, True, None, now=NOW) == "purchase-v1"
    doc = release(db, "purchase", "2026.10", "购买条款正文")
    with pytest.raises(AppError) as error:
        purchase_terms_checker(db, 7, False, "2026.10", now=NOW)
    assert error.value.code == ErrorCode.BUSINESS_RULE and error.value.data["document_id"] == doc.id
    with pytest.raises(AppError) as error:
        purchase_terms_checker(db, 7, True, "2026.01", now=NOW)
    assert error.value.data["reason"] == "terms_outdated" and error.value.data["version"] == "2026.10"
    assert purchase_terms_checker(db, 7, True, "2026.10", now=NOW) == "2026.10"
    assert purchase_terms_checker(db, 7, True, None, now=NOW) == "2026.10"  # 同版本不重复记同意
    consents = db.execute(select(legal.UserConsent).where(legal.UserConsent.kind == "purchase")).scalars().all()
    assert len(consents) == 1 and consents[0].channel == "order" and consents[0].content_sha256 == content_hash("购买条款正文")


def test_purchase_terms_checker_wired_into_order():
    import order
    from order import ports

    try:
        assert legal.install_purchase_terms_checker() is True
        assert ports._terms is legal.purchase_terms_checker
    finally:
        order.set_terms_checker(None)
