"""数据库：引擎、会话、模型基类与通用 mixin。

数据库规约（SPEC §3.5，参考阿里规约）：
- 每张表必备 id（bigint 自增）、created_at、updated_at；后台对象另有 created_by / updated_by。
- 表名小写蛇形、统一复数（与现有模块一致）；字段不以数字开头；禁止外键约束，关联列必须建索引；单表索引 ≤ 5。
- 金额整数分（*_cents），折扣万分比（*_bp）；时间 UTC；状态为小写蛇形字符串。
- 软删除用 deleted_at；唯一约束改为部分唯一索引（Postgres）或在服务层校验。
- 禁止 select-then-update；库存、余额、状态一律条件 UPDATE 并检查 rowcount。
- 约束命名固定（见 NAMING），便于 Alembic 生成可读迁移。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Integer, MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """所有模块共用的 ORM 基类。"""

    metadata = MetaData(naming_convention=NAMING)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


BigId = BigInteger().with_variant(Integer, "sqlite")


class BigIdMixin:
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)


_SessionFactory: sessionmaker[Session] | None = None
_Engine: Engine | None = None


def init_engine(url: str, pool_size: int = 20, statement_timeout_ms: int = 5000, echo: bool = False) -> Engine:
    """创建引擎并绑定会话工厂。sqlite 内存库用 StaticPool 共享连接；Postgres 设置语句超时。"""
    global _SessionFactory, _Engine
    kwargs: dict = {"future": True, "echo": echo, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_size"] = pool_size
        kwargs["max_overflow"] = pool_size // 2
        kwargs["pool_recycle"] = 1800
    engine = create_engine(url, **kwargs)
    if url.startswith("postgresql"):
        @event.listens_for(engine, "connect")
        def _set_timeout(dbapi_conn, _record):  # pragma: no cover - 仅 Postgres
            cur = dbapi_conn.cursor()
            cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
            cur.close()
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=OFF")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
    _Engine = engine
    _SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return engine


def get_engine() -> Engine:
    if _Engine is None:
        raise RuntimeError("database not initialised; call init_engine() first")
    return _Engine


def get_session_factory() -> sessionmaker[Session]:
    if _SessionFactory is None:
        raise RuntimeError("database not initialised; call init_engine() first")
    return _SessionFactory


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个会话；异常回滚；结束关闭。事务边界由服务层显式 commit。"""
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def transaction(db: Session) -> Iterator[Session]:
    """一个用例一个事务：成功 commit，失败 rollback 并重新抛出。"""
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def supports_skip_locked(db: Session) -> bool:
    bind = db.get_bind()
    return bind.dialect.name in {"postgresql", "mysql"}
