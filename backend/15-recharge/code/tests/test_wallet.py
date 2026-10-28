"""储值钱包：幂等充值、拆分、余额不足、并发不为负、原路冲回、退卡只退本金、流水对账。"""
import random
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import wallet
from luzback import AppError, Base, ErrorCode
from luzback.testing import memory_session
from wallet import PRINCIPAL_FIRST, WalletAccount, consume, ledger_matches_balance, recharge, refund_card, reverse, split


@pytest.fixture()
def db():
    session = memory_session(wallet)
    yield session
    session.close()


def balance(db, user_id=1):
    db.expire_all()
    from sqlalchemy import select
    account = db.execute(select(WalletAccount).where(WalletAccount.user_id == user_id)).scalar_one()
    return account.principal_cents, account.bonus_cents


def test_recharge_is_idempotent(db):
    recharge(db, 1, "pay:1", 100000, 20000)
    recharge(db, 1, "pay:1", 100000, 20000)
    db.commit()
    assert balance(db) == (100000, 20000)


def test_split_policies_sum_exactly():
    assert split(1200, 1000, 200) == (1000, 200)
    assert split(600, 1000, 200) == (500, 100)
    assert split(600, 1000, 200, PRINCIPAL_FIRST) == (600, 0)
    rng = random.Random(5)
    for _ in range(500):
        p, b = rng.randint(0, 10**6), rng.randint(0, 10**6)
        if p + b == 0:
            continue
        amount = rng.randint(1, p + b)
        fp, fb = split(amount, p, b)
        assert fp + fb == amount and 0 <= fp <= p and 0 <= fb <= b


def test_consume_reverse_and_insufficient(db):
    recharge(db, 1, "pay:1", 1000, 200)
    entry = consume(db, 1, "order:1", 600)
    assert (entry.principal_delta, entry.bonus_delta) == (-500, -100)
    assert consume(db, 1, "order:1", 600).id == entry.id  # 幂等
    with pytest.raises(AppError) as error:
        consume(db, 1, "order:2", 601)
    assert error.value.code == ErrorCode.INSUFFICIENT_BALANCE
    reverse(db, 1, "order:1")
    reverse(db, 1, "order:1")
    db.commit()
    assert balance(db) == (1000, 200) and ledger_matches_balance(db, 1)


def test_refund_card_returns_principal_only_and_closes(db):
    recharge(db, 1, "pay:1", 1000, 300)
    consume(db, 1, "order:1", 650)
    assert refund_card(db, 1, "card:1") == 500
    assert refund_card(db, 1, "card:1") == 500  # 幂等
    db.commit()
    assert balance(db) == (0, 0) and ledger_matches_balance(db, 1)
    with pytest.raises(AppError):
        consume(db, 1, "order:9", 1)


def test_concurrent_consumption_never_goes_negative(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'wallet.db'}", future=True, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        recharge(s, 1, "pay:1", 1000, 0)
        s.commit()

    def spend(i: int) -> str:
        with factory() as session:
            try:
                consume(session, 1, f"order:{i}", 100)
                session.commit()
                return "ok"
            except AppError as exc:
                session.rollback()
                return exc.code.name

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(spend, range(30)))
    assert results.count("ok") == 10
    with factory() as s:
        assert balance(s) == (0, 0) and ledger_matches_balance(s, 1)
    engine.dispose()
