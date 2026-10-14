"""Proves parallel purchases can never overspend a wallet.

Needs real row locks, so it only runs against Postgres:
  docker compose up -d db
  DATABASE_URL=postgresql+psycopg://allowance:allowance@localhost:5432/allowance pytest tests/test_concurrency.py -v
"""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.orm import sessionmaker

from app import ledger
from app.db import Base, make_engine

PG_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL.startswith("postgresql"), reason="needs Postgres for row locks")

PARALLEL = 50


def test_parallel_spends_never_overspend():
    engine = make_engine(PG_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session() as s:
        ledger.transfer(s, ledger.EXTERNAL, "agent_wallet:1", 20, "fund")
        s.commit()

    def spend(i):
        with Session() as s:
            try:
                ledger.transfer(s, "agent_wallet:1", "vendor_payable:1", 1, f"spend-{i}")
                s.commit()
                return True
            except ledger.InsufficientFunds:
                s.rollback()
                return False

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        results = list(pool.map(spend, range(PARALLEL)))

    with Session() as s:
        assert sum(results) == 20  # exactly the balance was spent
        assert ledger.balance(s, "agent_wallet:1") == 0
        assert ledger.balance(s, "vendor_payable:1") == 20
    engine.dispose()


def test_parallel_first_payments_to_a_new_account_do_not_crash():
    """Regression: many purchases hitting a vendor account that doesn't exist yet
    used to race on creating it and fail with a duplicate-key error."""
    engine = make_engine(PG_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        for w in range(PARALLEL):
            ledger.transfer(s, ledger.EXTERNAL, f"agent_wallet:{w}", 1, f"fund-{w}")
        s.commit()

    def first_payment(i):
        with Session() as s:
            ledger.transfer(s, f"agent_wallet:{i}", "vendor_payable:new", 1, f"pay-{i}")
            s.commit()

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        list(pool.map(first_payment, range(PARALLEL)))

    with Session() as s:
        assert ledger.balance(s, "vendor_payable:new") == PARALLEL
    engine.dispose()
