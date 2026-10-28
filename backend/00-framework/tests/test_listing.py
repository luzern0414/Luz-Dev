"""列表查询标准：筛选、排序、分页、关键词、游标。"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from luzback.db import Base, BigIdMixin
from luzback.errors import AppError
from luzback.listing import Field, ListSpec, escape_like
from luzback.testing import memory_session

SECRET = "test-secret"
UTC = timezone.utc


class Ticket(BigIdMixin, Base):
    __tablename__ = "listing_tickets"
    title: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20))
    amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


SPEC = ListSpec(
    fields={
        "status": Field(Ticket.status, ops=("eq", "in"), choices=frozenset({"open", "paid", "refunded"}), sortable=True),
        "amount_cents": Field(Ticket.amount_cents, "int", ops=("eq", "range", "null"), sortable=True),
        "created_at": Field(Ticket.created_at, "datetime", ops=("range",), sortable=True),
    },
    id_column=Ticket.id,
    keyword_columns=(Ticket.title,),
    max_range_days=31,
)


@pytest.fixture()
def db():
    session = memory_session()
    Base.metadata.create_all(session.get_bind(), tables=[Ticket.__table__])
    rng = random.Random(7)
    start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    for i in range(57):
        session.add(Ticket(
            title=f"票据{i:02d}" + ("_折扣%" if i % 10 == 0 else ""),
            status=("open", "paid", "refunded")[i % 3],
            amount_cents=None if i % 7 == 0 else rng.choice([100, 200, 300, 500]),
            created_at=start + timedelta(hours=i * 7),
        ))
    session.commit()
    return session


def base():
    return select(Ticket)


def test_eq_in_and_choices(db):
    paid = SPEC.page(db, base(), {"status": "paid"})
    assert paid["total"] == 19 and all(t.status == "paid" for t in paid["items"])
    both = SPEC.page(db, base(), {"status": "paid, refunded"})
    assert both["total"] == 38
    with pytest.raises(AppError) as err:
        SPEC.page(db, base(), {"status": "deleted"})
    assert err.value.code == 4001


def test_unknown_param_rejected(db):
    with pytest.raises(AppError) as err:
        SPEC.page(db, base(), {"stauts": "paid"})
    assert "stauts" in err.value.message


def test_empty_value_means_not_given(db):
    assert SPEC.page(db, base(), {"status": "", "keyword": "  "})["total"] == 57


def test_numeric_range_inclusive_and_null(db):
    page = SPEC.page(db, base(), {"amount_cents_min": "200", "amount_cents_max": "300", "page_size": "100"})
    assert page["items"] and all(200 <= t.amount_cents <= 300 for t in page["items"])
    nulls = SPEC.page(db, base(), {"amount_cents_null": "true"})
    assert nulls["total"] == 9
    with pytest.raises(AppError):
        SPEC.page(db, base(), {"amount_cents_min": "500", "amount_cents_max": "100"})


def test_date_range_uses_store_timezone_whole_days(db):
    # 10-02 in Asia/Shanghai = [10-01 16:00Z, 10-02 16:00Z)
    page = SPEC.page(db, base(), {"created_at_from": "2026-10-02", "created_at_to": "2026-10-02", "page_size": "100"})
    low, high = datetime(2026, 10, 1, 16, tzinfo=UTC), datetime(2026, 10, 2, 16, tzinfo=UTC)
    expected = [i for i in range(57) if low <= datetime(2026, 10, 1, tzinfo=UTC) + timedelta(hours=i * 7) < high]
    assert page["total"] == len(expected) > 0
    with pytest.raises(AppError):
        SPEC.page(db, base(), {"created_at_from": "2026-10-01", "created_at_to": "2026-12-31"})  # > 31 days
    with pytest.raises(AppError):
        SPEC.page(db, base(), {"created_at_from": "2026-10-05", "created_at_to": "2026-10-01"})


def test_keyword_escapes_wildcards(db):
    assert escape_like("50%_off") == "50\\%\\_off"
    hits = SPEC.page(db, base(), {"keyword": "_折扣%"})
    assert hits["total"] == 6
    assert SPEC.page(db, base(), {"keyword": "%"})["total"] == 6  # % is literal, not "everything"
    with pytest.raises(AppError):
        SPEC.page(db, base(), {"keyword": "x" * 51})


def test_sort_whitelist_and_stable_tiebreak(db):
    page = SPEC.page(db, base(), {"sort": "amount_cents", "page_size": "100"})
    amounts = [t.amount_cents for t in page["items"]]
    present = [a for a in amounts if a is not None]
    assert present == sorted(present) and amounts[-9:] == [None] * 9  # NULLS LAST
    same = [t.id for t in page["items"] if t.amount_cents == 100]
    assert same == sorted(same)  # id breaks ties in the same direction
    for bad_sort in ("title", "status,status", "status,amount_cents,created_at,-id,title"):
        with pytest.raises(AppError):
            SPEC.page(db, base(), {"sort": bad_sort})


def test_page_limits(db):
    page = SPEC.page(db, base(), {"page": "2", "page_size": "1000"})
    assert page["page_size"] == 100 and page["items"] == []
    with pytest.raises(AppError):
        SPEC.page(db, base(), {"page": "101", "page_size": "100"})
    with pytest.raises(AppError):
        SPEC.page(db, base(), {"page": "0"})


@pytest.mark.parametrize("sort", ["-created_at", "amount_cents", "-amount_cents", "status,-amount_cents", "-status,amount_cents"])
def test_cursor_walks_every_row_once(db, sort):
    full = [t.id for t in SPEC.page(db, base(), {"sort": sort, "page_size": "100"})["items"]]
    seen, cursor = [], None
    for _ in range(30):
        params = {"sort": sort, "limit": "8"}
        if cursor:
            params["cursor"] = cursor
        page = SPEC.cursor(db, base(), params, SECRET)
        seen += [t.id for t in page["items"]]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert seen == full


def test_cursor_bound_to_filters_and_signed(db):
    first = SPEC.cursor(db, base(), {"status": "paid", "limit": "5"}, SECRET)
    token = first["next_cursor"]
    with pytest.raises(AppError):
        SPEC.cursor(db, base(), {"status": "open", "limit": "5", "cursor": token}, SECRET)
    with pytest.raises(AppError):
        SPEC.cursor(db, base(), {"status": "paid", "limit": "5", "cursor": token}, "other-secret")
    forged = token.split(".")[0] + ".0000000000000000"
    with pytest.raises(AppError):
        SPEC.cursor(db, base(), {"status": "paid", "cursor": forged}, SECRET)
    # changing only the page size keeps the cursor valid
    nxt = SPEC.cursor(db, base(), {"status": "paid", "limit": "7", "cursor": token}, SECRET)
    assert nxt["items"][0].id not in [t.id for t in first["items"]]


def test_export_csv_safe_and_filtered(db):
    from luzback.export import cents_to_yuan, export_csv, safe_cell

    assert safe_cell("＝SUM(A1)") == "'＝SUM(A1)" and safe_cell("=1+1") == "'=1+1" and safe_cell("-5") == "'-5" and safe_cell(-5) == "-5"
    assert cents_to_yuan(12345) == "123.45" and cents_to_yuan(-5) == "-0.05"
    stream = export_csv(db, SPEC, base(), {"status": "paid"}, ["标题", "金额（元）", "创建时间"],
                        lambda t: [t.title, cents_to_yuan(t.amount_cents), t.created_at])
    text = b"".join(stream).decode("utf-8")
    assert text.startswith("﻿标题,金额（元）,创建时间")
    assert len(text.strip().splitlines()) == 1 + 19
    with pytest.raises(AppError) as err:
        export_csv(db, SPEC, base(), {}, ["标题"], lambda t: [t.title], hard_limit=10)
    assert err.value.code == 4225


def test_cursor_expires(db, monkeypatch):
    import luzback.listing as listing

    token = SPEC.cursor(db, base(), {"limit": "5"}, SECRET)["next_cursor"]
    real = listing._time.time
    monkeypatch.setattr(listing._time, "time", lambda: real() + listing.CURSOR_TTL_SECONDS + 5)
    with pytest.raises(AppError):
        SPEC.cursor(db, base(), {"limit": "5", "cursor": token}, SECRET)
