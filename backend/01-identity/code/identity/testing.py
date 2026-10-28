"""给其他模块测试用的会员登录态工具（不连微信）。

    from identity.testing import member_token
    token = member_token(db)                      # 新建会员并开会话，返回 access 令牌
    token = member_token(db, user_id=existing_id)  # 给已有会员开会话
    headers = {"Authorization": f"Bearer {token}"}

令牌与真实登录完全一致（aud / sid / 会话校验都能通过）；db 必须已建 identity 的表（memory_session(identity, ...)）。
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode

from .models import STATUS_ACTIVE, User
from .rules import new_member_no
from .service import issue_tokens


def create_member(db: Session, openid: str | None = None, nickname: str = "") -> User:
    user = User(member_no=new_member_no(), openid=openid or f"test-{uuid.uuid4().hex}", nickname=nickname, status=STATUS_ACTIVE)
    db.add(user)
    db.commit()
    return user


def member_tokens(db: Session, user_id: int | None = None, openid: str | None = None) -> dict:
    """返回完整令牌对（含 user_id、access_token、refresh_token、session_id）。"""
    if user_id is None:
        user = create_member(db, openid)
    else:
        user = db.get(User, user_id)
        if user is None:
            raise AppError(ErrorCode.NOT_FOUND, "user not found")
    return issue_tokens(db, user, channel="test")


def member_token(db: Session, user_id: int | None = None, openid: str | None = None) -> str:
    return member_tokens(db, user_id, openid)["access_token"]
