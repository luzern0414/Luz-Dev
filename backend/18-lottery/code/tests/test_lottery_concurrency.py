"""并发（文件 SQLite + 50 线程）：奖品不超发、次数不超用、同键只抽一次。"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

import lottery  # noqa: F401  注册模型
from lottery import hooks, service
from lottery.hooks import FixedRandom
from lottery.models import LotteryActivity, LotteryChanceAccount, LotteryDraw, LotteryPrize
from lottery_testkit import ADMIN, HIT, NOW, key, make_activity, prize_id, prizes, setup_hooks
from luzback import AppError, Base


@pytest.fixture()
def factory(tmp_path):
    setup_hooks()
    engine = create_engine(f"sqlite:///{tmp_path / 'lottery-race.db'}", future=True,
                           connect_args={"check_same_thread": False, "timeout": 60})

    # pysqlite 默认 DEFERRED 事务：先读后写时“读锁升级写锁”遇到另一写者会立即 SQLITE_BUSY（不等 timeout，
    # 这是偶发 "database is locked" 的根因）。改为 BEGIN IMMEDIATE：写者在事务开始处排队等锁，
    # 语义接近 Postgres 的行锁等待；业务的条件 UPDATE 仍是唯一的裁决手段。
    @event.listens_for(engine, "connect")
    def _no_pysqlite_begin(dbapi_conn, _record):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_immediate(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()
    setup_hooks()


def _run(factory, jobs):
    def one(job):
        session = factory()
        try:
            return job(session)
        except AppError as exc:
            session.rollback()
            return exc.code.name
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=16) as pool:
        return list(pool.map(one, jobs))


def test_50_members_race_for_5_prizes_never_oversell(factory):
    plist = prizes()
    plist[2]["stock_total"] = 5  # 实物 5 件
    with factory() as s:
        act = make_activity(s, prize_list=plist)
    hooks.set_random_source(FixedRandom(HIT[3]))  # 每次都命中实物
    jobs = [lambda s, m=m: service.draw(s, act["id"], 1000 + m, key("race"), now=NOW) for m in range(50)]
    results = _run(factory, jobs)
    wins = [r for r in results if isinstance(r, dict) and r["won"]]
    fallbacks = [r for r in results if isinstance(r, dict) and r["fallback_reason"] == "out_of_stock"]
    assert len(wins) == 5 and len(fallbacks) == 45
    with factory() as s:
        prize = s.get(LotteryPrize, prize_id(act, 3))
        assert prize.stock_left == 0 and prize.win_count == 5
        assert s.get(LotteryActivity, act["id"]).spent_cents == 5 * 80_000
        assert s.execute(select(func.count()).select_from(LotteryDraw)).scalar_one() == 50


def test_one_member_50_threads_never_exceed_chances(factory):
    with factory() as s:
        act = make_activity(s, free_daily_chances=0)
        service.grant_chances(s, act["id"], 7, 3, "event", "gift")
    jobs = [lambda s: service.draw(s, act["id"], 7, key("burst"), now=NOW) for _ in range(50)]
    results = _run(factory, jobs)
    ok = [r for r in results if isinstance(r, dict)]
    assert len(ok) == 3
    assert set(r for r in results if not isinstance(r, dict)) <= {"LIMIT_REACHED"}
    with factory() as s:
        acc = s.execute(select(LotteryChanceAccount).where(LotteryChanceAccount.member_id == 7)).scalar_one()
        assert acc.used == 3 and acc.granted == 3 and acc.draws == 3


def test_daily_limit_holds_under_race(factory):
    with factory() as s:
        act = make_activity(s, free_daily_chances=1, limit_daily=2)
        service.grant_chances(s, act["id"], 8, 20, "event", "gift")
    results = _run(factory, [lambda s: service.draw(s, act["id"], 8, key("daily"), now=NOW) for _ in range(50)])
    assert sum(isinstance(r, dict) for r in results) == 2
    with factory() as s:
        assert s.execute(select(func.count()).select_from(LotteryDraw).where(LotteryDraw.member_id == 8)).scalar_one() == 2


def test_same_key_concurrent_draws_only_once(factory):
    with factory() as s:
        act = make_activity(s, free_daily_chances=0)
        service.grant_chances(s, act["id"], 9, 10, "event", "gift")
    k = key("dup")
    results = _run(factory, [lambda s: service.draw(s, act["id"], 9, k, now=NOW) for _ in range(20)])
    oks = [r for r in results if isinstance(r, dict)]
    assert oks and all(r == oks[0] for r in oks)
    assert set(r for r in results if not isinstance(r, dict)) <= {"DUPLICATE_REQUEST"}
    with factory() as s:
        assert s.execute(select(func.count()).select_from(LotteryDraw).where(LotteryDraw.member_id == 9)).scalar_one() == 1
        acc = s.execute(select(LotteryChanceAccount).where(LotteryChanceAccount.member_id == 9)).scalar_one()
        assert acc.used == 1


def test_concurrent_event_grants_are_idempotent(factory):
    with factory() as s:
        act = make_activity(s, earn_payment_min_cents=100, earn_payment_chances=1)
    payload = {"payment_id": 555, "user_id": 21, "amount_cents": 1000}
    results = _run(factory, [lambda s, i=i: service.handle_event(s, "payment.succeeded", payload, f"ev-{i}", now=NOW)
                             for i in range(30)])
    assert results.count("granted:1") == 1
    with factory() as s:
        acc = s.execute(select(LotteryChanceAccount).where(LotteryChanceAccount.member_id == 21)).scalar_one()
        assert acc.granted == 1
    assert ADMIN.id == 1
