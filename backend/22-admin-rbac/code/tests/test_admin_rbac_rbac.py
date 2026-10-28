"""RBAC：种子、注册表、默认拒绝、门店范围、职责分离、防越权、最后一名超级管理员、角色规则、员工管理与列表。"""
from __future__ import annotations

import pytest
from sqlalchemy import Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from admin_rbac import (
    Staff, apply_store_scope, assert_in_scope, assert_not_self, assert_permission, declare_permissions, ensure_seed,
    log_sensitive_read, registered,
)
from admin_rbac import auth, service
from admin_rbac.access import load_staff
from admin_rbac.models import AdminPermission, AdminRole, AdminUser
from admin_rbac.permissions import declare_permission
from luzback import AppError, Base, ErrorCode
from luzback.audit import AuditLog
from luzback.outbox import OutboxEvent

from admin_rbac_testkit import actor, login, make_user, new_db, reset_hooks, role_id, staff_of, token_of


class ScopedThing(Base):
    __tablename__ = "admin_rbac_test_things"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(20))


@pytest.fixture()
def db():
    reset_hooks()
    session = new_db()
    yield session
    session.close()
    reset_hooks()


def err(fn) -> AppError:
    with pytest.raises(AppError) as exc:
        fn()
    return exc.value


def custom_role(db, name: str, codes: list[str], boss_id: int) -> int:
    rid = service.create_role(db, actor(db, boss_id), name, codes)["id"]
    db.commit()
    return rid


# ---------- 种子与注册表 ----------

def test_seed_roles_and_permissions(db):
    codes = {r.builtin_code for r in db.execute(select(AdminRole)).scalars()}
    assert codes == {"super_admin", "store_manager", "front_desk", "finance", "operator"}
    assert db.get(AdminPermission, "staff:write") is not None
    assert ensure_seed(db) == {"permissions": 0, "roles": 0}  # 幂等


def test_registry_declare_and_validate():
    declare_permissions("demo", [("demo:approve", "演示审批")])
    assert any(p.code == "demo:approve" and p.module == "demo" for p in registered())
    with pytest.raises(ValueError):
        declare_permission("Bad Code")
    from admin_rbac.api import require_permission

    require_permission("demo:auto_registered")
    assert any(p.code == "demo:auto_registered" for p in registered())


def test_new_declared_codes_synced_on_seed(db):
    declare_permissions("demo2", [("demo2:run", "演示")])
    ensure_seed(db)
    assert db.get(AdminPermission, "demo2:run").module == "demo2"


# ---------- 默认拒绝、通配、范围、职责分离 ----------

def test_deny_by_default(db):
    nobody = make_user(db, "nobody", roles=())
    staff = actor(db, nobody.id)
    assert staff.permissions == frozenset()
    assert err(lambda: assert_permission(staff, "order:read")).code == ErrorCode.FORBIDDEN


def test_super_admin_holds_everything(db):
    boss = make_user(db, "boss")
    staff = actor(db, boss.id)
    assert staff.has("order:refund") and staff.has("future:anything")
    assert_permission(staff, "staff:write", "role:write")


def test_role_permissions_take_effect_immediately(db):
    boss = make_user(db, "boss")
    rid = custom_role(db, "临时", ["order:read", "order:remark"], boss.id)
    clerk = make_user(db, "clerk", roles=(rid,))
    token = token_of(db, "clerk")
    assert staff_of(db, token).has("order:remark")
    service.update_role(db, actor(db, boss.id), rid, permissions=["order:read"])
    db.commit()
    assert not staff_of(db, token).has("order:remark")


def test_store_scope_helpers(db):
    db.add_all([ScopedThing(id=i, store_id=s, name=f"t{i}") for i, s in ((1, 1), (2, 2), (3, 3))])
    db.commit()
    everyone = Staff(id=1, username="a", all_stores=True)
    some = Staff(id=2, username="b", store_ids=(1, 3))
    none = Staff(id=3, username="c")
    q = select(ScopedThing.id).order_by(ScopedThing.id)
    assert db.execute(apply_store_scope(q, everyone, ScopedThing.store_id)).scalars().all() == [1, 2, 3]
    assert db.execute(apply_store_scope(q, some, ScopedThing.store_id)).scalars().all() == [1, 3]
    assert db.execute(apply_store_scope(q, none, ScopedThing.store_id)).scalars().all() == []
    assert_in_scope(some, 3)
    assert err(lambda: assert_in_scope(some, 2)).code == ErrorCode.SCOPE_FORBIDDEN
    assert err(lambda: assert_in_scope(some, None)).code == ErrorCode.SCOPE_FORBIDDEN
    assert_in_scope(everyone, None)


def test_separation_of_duties_helper():
    me = Staff(id=7, username="a")
    assert_not_self(me, 8, "approve")
    e = err(lambda: assert_not_self(me, "7", "approve"))
    assert e.code == ErrorCode.FORBIDDEN and e.data == {"reason": "separation_of_duties"}


def test_sensitive_read_helper(db):
    boss = make_user(db, "boss")
    log_sensitive_read(db, actor(db, boss.id), "member", 42, ["phone", "id_card"], reason="客诉回访")
    db.commit()
    row = db.execute(select(AuditLog).where(AuditLog.action == "member.sensitive_read")).scalar_one()
    assert row.resource_id == "42" and '"phone"' in row.after_json and row.reason == "客诉回访"


# ---------- 防越权 ----------

def test_cannot_grant_permissions_you_do_not_hold(db):
    boss = make_user(db, "boss")
    hr = custom_role(db, "人事", ["staff:read", "staff:write", "order:read", "role:write"], boss.id)
    hr_user = make_user(db, "hr", roles=(hr,))
    me = actor(db, hr_user.id)
    e = err(lambda: service.create_role(db, me, "越权", ["order:read", "order:close"]))
    assert e.code == ErrorCode.FORBIDDEN and e.data["extra"] == ["order:close"]
    db.rollback()
    assert service.create_role(db, me, "子集", ["order:read"])["permissions"] == ["order:read"]
    db.commit()
    # 不能分配超出自己权限的角色
    e = err(lambda: service.create_staff(db, me, "clerk1", role_ids=[role_id(db, "front_desk")], all_stores=True))
    assert e.code == ErrorCode.FORBIDDEN and e.data["reason"] == "privilege_escalation"
    db.rollback()
    e = err(lambda: service.create_staff(db, me, "clerk2", role_ids=[role_id(db, "super_admin")], all_stores=True))
    assert e.code == ErrorCode.FORBIDDEN
    db.rollback()
    # 不能编辑自己持有的角色来给自己加权限
    e = err(lambda: service.update_role(db, me, hr, permissions=["staff:read", "staff:write", "order:read", "role:write", "audit:read"]))
    assert e.code == ErrorCode.FORBIDDEN


def test_cannot_grant_stores_outside_scope(db):
    boss = make_user(db, "boss")
    hr = custom_role(db, "人事", ["staff:read", "staff:write"], boss.id)
    hr_user = make_user(db, "hr", roles=(hr,), all_stores=False, store_ids=(1, 2))
    me = actor(db, hr_user.id)
    assert err(lambda: service.create_staff(db, me, "cc1", role_ids=[hr], all_stores=True)).code == ErrorCode.FORBIDDEN
    db.rollback()
    assert err(lambda: service.create_staff(db, me, "cc2", role_ids=[hr], store_ids=[2, 3])).code == ErrorCode.FORBIDDEN
    db.rollback()
    created = service.create_staff(db, me, "cc3", role_ids=[hr], store_ids=[2])
    db.commit()
    assert created["store_ids"] == [2] and created["all_stores"] is False


def test_cannot_manage_more_privileged_staff(db):
    boss = make_user(db, "boss")
    hr = custom_role(db, "人事", ["staff:read", "staff:write"], boss.id)
    hr_user = make_user(db, "hr", roles=(hr,))
    me = actor(db, hr_user.id)
    assert err(lambda: service.reset_password(db, me, boss.id)).code == ErrorCode.FORBIDDEN
    manager = make_user(db, "manager", roles=("store_manager",))
    assert err(lambda: service.disable_staff(db, me, manager.id)).code == ErrorCode.FORBIDDEN


def test_cannot_act_on_self(db):
    boss = make_user(db, "boss")
    me = actor(db, boss.id)
    for fn in (lambda: service.disable_staff(db, me, boss.id), lambda: service.delete_staff(db, me, boss.id),
               lambda: service.reset_password(db, me, boss.id),
               lambda: service.set_staff_access(db, me, boss.id, role_ids=[], all_stores=True)):
        assert err(fn).code == ErrorCode.FORBIDDEN


def test_last_super_admin_protected(db):
    boss = make_user(db, "boss")
    system = Staff(id=0, username="system", permissions=frozenset({"*"}), all_stores=True, is_super_admin=True)
    for fn in (lambda: service.disable_staff(db, system, boss.id), lambda: service.delete_staff(db, system, boss.id),
               lambda: service.set_staff_access(db, system, boss.id, role_ids=[], all_stores=True)):
        e = err(fn)
        assert e.code == ErrorCode.BUSINESS_RULE and e.data == {"reason": "last_super_admin"}
        db.rollback()
    boss2 = make_user(db, "boss2")
    service.disable_staff(db, actor(db, boss.id), boss2.id)  # 还剩 boss
    db.commit()
    assert db.get(AdminUser, boss2.id).status == "disabled"


def test_bootstrap_super_admin(db):
    result = service.bootstrap_super_admin(db, "owner", "店主")
    assert result["temporary_password"]
    assert login(db, "owner", result["temporary_password"])["must_change_password"] is True
    assert err(lambda: service.bootstrap_super_admin(db, "owner2")).code == ErrorCode.CONFLICT


def test_bootstrap_rejects_default_names():
    reset_hooks()
    session = new_db()
    assert err(lambda: service.bootstrap_super_admin(session, "admin")).code == ErrorCode.BAD_REQUEST
    session.close()


# ---------- 角色 ----------

def test_role_rules(db):
    boss = make_user(db, "boss")
    me = actor(db, boss.id)
    super_id = role_id(db, "super_admin")
    assert err(lambda: service.update_role(db, me, super_id, permissions=[])).code == ErrorCode.BUSINESS_RULE
    assert err(lambda: service.delete_role(db, me, role_id(db, "front_desk"))).code == ErrorCode.BUSINESS_RULE
    # 内置非超管角色可编辑
    assert service.update_role(db, me, role_id(db, "front_desk"), permissions=["order:read"])["permissions"] == ["order:read"]
    db.commit()
    rid = custom_role(db, "夜班", ["order:read"], boss.id)
    make_user(db, "night", roles=(rid,))
    assert err(lambda: service.delete_role(db, me, rid)).code == ErrorCode.CONFLICT
    assert err(lambda: service.create_role(db, me, "夜班", ["order:read"])).code == ErrorCode.CONFLICT
    db.rollback()
    assert err(lambda: service.create_role(db, me, "x", ["nope:nope"])).code == ErrorCode.BAD_REQUEST
    empty = custom_role(db, "空角色", [], boss.id)
    assert service.delete_role(db, me, empty) == {"id": empty, "deleted": True}
    db.commit()
    page = service.list_roles(db, {"builtin": "true", "sort": "name"})
    assert page["total"] == 5 and all(r["builtin"] for r in page["items"])
    sa = [r for r in page["items"] if r["builtin_code"] == "super_admin"][0]
    assert sa["permissions"] == ["*"] and sa["editable"] is False and sa["user_count"] == 1


# ---------- 员工管理 ----------

def test_create_staff_phone_encrypted_masked_and_revealed(db):
    boss = make_user(db, "boss")
    me = actor(db, boss.id)
    created = service.create_staff(db, me, "Clerk.One", "李四", phone="13800138000",
                                   role_ids=[role_id(db, "front_desk")], store_ids=[1])
    db.commit()
    assert created["username"] == "clerk.one" and created["phone"] == "138****8000"
    row = db.get(AdminUser, created["id"])
    assert "13800138000" not in (row.phone_enc or "") and len(row.phone_hash) == 64
    assert service.reveal_phone(db, me, row.id, "核对排班")["phone"] == "13800138000"
    assert db.execute(select(AuditLog).where(AuditLog.action == "staff.sensitive_read")).scalar_one()
    assert err(lambda: service.reveal_phone(db, me, row.id, "")).code == ErrorCode.BAD_REQUEST
    assert service.list_staff(db, me, {"phone": "13800138000"})["total"] == 1
    events = db.execute(select(OutboxEvent.topic)).scalars().all()
    assert "staff.created" in events


def test_create_staff_validation(db):
    boss = make_user(db, "boss")
    me = actor(db, boss.id)
    assert err(lambda: service.create_staff(db, me, "root", all_stores=True)).code == ErrorCode.BAD_REQUEST
    assert err(lambda: service.create_staff(db, me, "boss", all_stores=True)).code == ErrorCode.CONFLICT
    db.rollback()
    assert err(lambda: service.create_staff(db, me, "x1y", role_ids=[999])).code == ErrorCode.BAD_REQUEST
    assert err(lambda: service.create_staff(db, me, "x1y", phone="123")).code == ErrorCode.BAD_REQUEST


def test_disable_enable_unlock_delete(db):
    boss = make_user(db, "boss")
    me = actor(db, boss.id)
    clerk = make_user(db, "clerk", roles=("front_desk",))
    service.disable_staff(db, me, clerk.id, reason="离职")
    db.commit()
    assert err(lambda: service.disable_staff(db, me, clerk.id)).code == ErrorCode.CONFLICT
    db.rollback()
    assert err(lambda: login(db, "clerk")).code == ErrorCode.UNAUTHORIZED
    service.enable_staff(db, me, clerk.id)
    db.commit()
    assert err(lambda: service.enable_staff(db, me, clerk.id)).code == ErrorCode.CONFLICT
    db.rollback()
    for _ in range(5):
        err(lambda: login(db, "clerk", "bad-password-x"))
    assert service.unlock_staff(db, me, clerk.id)["locked_until"] is None
    db.commit()
    assert login(db, "clerk")["token"]
    service.delete_staff(db, me, clerk.id, reason="清理")
    db.commit()
    assert db.get(AdminUser, clerk.id).deleted_at is not None
    assert err(lambda: login(db, "clerk")).code == ErrorCode.UNAUTHORIZED
    assert err(lambda: service.get_staff(db, me, clerk.id)).code == ErrorCode.NOT_FOUND
    assert err(lambda: service.create_staff(db, me, "clerk", all_stores=True)).code == ErrorCode.CONFLICT  # 账号名不复用
    db.rollback()
    actions = set(db.execute(select(AuditLog.action)).scalars())
    assert {"staff.disable", "staff.enable", "staff.unlock", "staff.delete"} <= actions


def test_reset_password_issues_temp_and_revokes(db):
    boss = make_user(db, "boss")
    clerk = make_user(db, "clerk", roles=("front_desk",))
    token = token_of(db, "clerk")
    result = service.reset_password(db, actor(db, boss.id), clerk.id, reason="忘记口令")
    db.commit()
    assert err(lambda: staff_of(db, token)).code == ErrorCode.UNAUTHORIZED
    assert err(lambda: login(db, "clerk")).code == ErrorCode.UNAUTHORIZED
    assert login(db, "clerk", result["temporary_password"])["must_change_password"] is True


def test_admin_revokes_staff_sessions(db):
    boss = make_user(db, "boss")
    clerk = make_user(db, "clerk", roles=("front_desk",))
    token = token_of(db, "clerk")
    assert len(service.staff_sessions(db, actor(db, boss.id), clerk.id)) == 1
    assert service.revoke_staff_sessions(db, actor(db, boss.id), clerk.id)["revoked"] == 1
    db.commit()
    assert err(lambda: staff_of(db, token)).code == ErrorCode.UNAUTHORIZED


def test_list_staff_filters_and_scope(db):
    boss = make_user(db, "boss", name="老板")
    make_user(db, "clerk.a", roles=("front_desk",), all_stores=False, store_ids=(1,), name="张三")
    make_user(db, "clerk.b", roles=("front_desk",), all_stores=False, store_ids=(2,), name="李四")
    make_user(db, "clerk.c", roles=("front_desk",), all_stores=False, store_ids=(1, 2), name="王五")
    me = actor(db, boss.id)
    assert service.list_staff(db, me, {})["total"] == 4
    assert [i["username"] for i in service.list_staff(db, me, {"keyword": "张"})["items"]] == ["clerk.a"]
    assert service.list_staff(db, me, {"role_id": str(role_id(db, "front_desk"))})["total"] == 3
    assert {i["username"] for i in service.list_staff(db, me, {"store_id": "1"})["items"]} == {"boss", "clerk.a", "clerk.c"}
    assert service.list_staff(db, me, {"status": "disabled"})["total"] == 0
    assert [i["username"] for i in service.list_staff(db, me, {"sort": "username", "page_size": "2"})["items"]] == ["boss", "clerk.a"]
    assert err(lambda: service.list_staff(db, me, {"bogus": "1"})).code == ErrorCode.BAD_REQUEST
    assert err(lambda: service.list_staff(db, me, {"status": "weird"})).code == ErrorCode.BAD_REQUEST
    # 只管门店 1 的人事：看不到门店 2 / 跨店 / 全门店员工
    hr = custom_role(db, "人事", ["staff:read", "staff:write", *service.BUILTIN_ROLES[2][3]], boss.id)
    hr_user = make_user(db, "hr", roles=(hr,), all_stores=False, store_ids=(1,))
    scoped = actor(db, hr_user.id)
    assert {i["username"] for i in service.list_staff(db, scoped, {})["items"]} == {"clerk.a", "hr"}
    clerk_b = db.execute(select(AdminUser).where(AdminUser.username == "clerk.b")).scalar_one()
    assert err(lambda: service.get_staff(db, scoped, clerk_b.id)).code == ErrorCode.NOT_FOUND
    assert err(lambda: service.get_staff(db, scoped, boss.id)).code == ErrorCode.NOT_FOUND
    clerk_a = db.execute(select(AdminUser).where(AdminUser.username == "clerk.a")).scalar_one()
    service.disable_staff(db, scoped, clerk_a.id)  # 范围内、权限不超过自己 → 可以管理
    db.commit()


def test_export_staff_masks_phone(db):
    boss = make_user(db, "boss")
    me = actor(db, boss.id)
    service.create_staff(db, me, "clerk", "=cmd|' /C calc'!A0", phone="13800138000", all_stores=True)
    db.commit()
    data = service.export_staff(db, me, {}).decode("utf-8-sig")
    assert "138****8000" in data and "13800138000" not in data
    assert "'=cmd" in data  # 公式注入防护
    assert db.execute(select(AuditLog).where(AuditLog.action == "staff.export")).scalar_one()


def test_load_staff_rejects_inactive(db):
    boss = make_user(db, "boss")
    clerk = make_user(db, "clerk", roles=("front_desk",))
    service.disable_staff(db, actor(db, boss.id), clerk.id)
    db.commit()
    assert err(lambda: load_staff(db, clerk.id)).code == ErrorCode.UNAUTHORIZED
    assert err(lambda: auth.authenticate(db, None)).code == ErrorCode.UNAUTHORIZED


# ---------- 模块通配授予 module:* ----------

def test_module_wildcard_grant_prefix_match(db):
    from admin_rbac.permissions import grants

    assert grants({"wallet:*"}, "wallet:adjust") and grants({"wallet:*"}, "wallet:future_code")
    assert not grants({"wallet:*"}, "wallet_x:read")  # 前缀到冒号为止，不是字符串前缀
    assert not grants({"wallet:*"}, "member_card:read") and not grants({"order:read"}, "order:close")
    assert grants({"*"}, "anything:else") and grants({"*"}, "wallet:*")
    assert not grants({"wallet:adjust", "wallet:read"}, "wallet:*")  # 持有全部现有码 ≠ 持有通配
    assert grants({"wallet:*"}, "wallet:*")


def test_wildcard_role_covers_codes_declared_later(db):
    boss = make_user(db, "boss")
    declare_permissions("demo3", [("demo3:read", "演示")])
    rid = custom_role(db, "演示通配", ["demo3:*"], boss.id)
    clerk = make_user(db, "clerk", roles=(rid,))
    staff = actor(db, clerk.id)
    assert staff.has("demo3:read")
    declare_permissions("demo3", [("demo3:approve", "后来新增")])
    assert staff.has("demo3:approve") and not staff.has("demo4:read")
    assert_permission(staff, "demo3:read", "demo3:approve")


def test_wildcard_grant_validation_and_escalation(db):
    boss = make_user(db, "boss")
    me = actor(db, boss.id)
    e = err(lambda: service.create_role(db, me, "未知模块", ["nosuchmodule:*"]))
    assert e.code == ErrorCode.BAD_REQUEST
    assert err(lambda: service.create_role(db, me, "全部", ["*"])).code == ErrorCode.BAD_REQUEST
    declare_permissions("demo5", [("demo5:read", "读"), ("demo5:write", "写")])
    exact = custom_role(db, "精确", ["demo5:read", "demo5:write", "role:write"], boss.id)
    holder = actor(db, make_user(db, "holder", roles=(exact,)).id)
    # 只持有现有精确码的人不能授予通配（通配会覆盖以后新增的码）
    e = err(lambda: service.create_role(db, holder, "越权通配", ["demo5:*"]))
    assert e.code == ErrorCode.FORBIDDEN and e.data["extra"] == ["demo5:*"]
    wild = custom_role(db, "通配", ["demo5:*", "role:write"], boss.id)
    wholder = actor(db, make_user(db, "wholder", roles=(wild,)).id)
    assert service.create_role(db, wholder, "子集", ["demo5:read"])["permissions"] == ["demo5:read"]
    assert service.create_role(db, wholder, "同级通配", ["demo5:*"])["permissions"] == ["demo5:*"]


def test_builtin_roles_store_wildcards_and_mfa_matches_wildcard(db):
    from admin_rbac.models import AdminRolePermission

    fin = role_id(db, "finance")
    codes = set(db.execute(select(AdminRolePermission.code).where(AdminRolePermission.role_id == fin)).scalars())
    assert {"payment:*", "wallet:*", "after_sale:*"} <= codes
    manager = actor(db, make_user(db, "manager", roles=("store_manager",)).id)
    assert manager.has("after_sale:approve") and manager.mfa_required  # after_sale:* 覆盖 MFA 清单里的 after_sale:approve
    clerk = actor(db, make_user(db, "clerk", roles=("front_desk",)).id)
    assert clerk.has("wallet:consume") and not clerk.has("wallet:adjust") and not clerk.mfa_required


def test_builtin_roles_reference_every_module_prefix():
    from admin_rbac.seed import BUILTIN_ROLES

    prefixes = {p.split(":", 1)[0] for _c, _n, _d, patterns, _m in BUILTIN_ROLES for p in patterns if p != "*"}
    for module in ("member", "order", "payment", "after_sale", "wallet", "coupon", "lottery", "notification", "member_card"):
        assert module in prefixes, module
