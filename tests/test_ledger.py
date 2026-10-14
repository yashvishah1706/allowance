import pytest
from sqlalchemy import func, select

from app import ledger
from app.models import LedgerEntry, Transfer


def test_transfer_moves_credits_and_entries_sum_to_zero(session):
    ledger.transfer(session, ledger.EXTERNAL, "human_pool", 100, "t1")
    ledger.transfer(session, "human_pool", "agent_wallet:1", 30, "t2")
    session.commit()
    assert ledger.balance(session, "human_pool") == 70
    assert ledger.balance(session, "agent_wallet:1") == 30
    assert ledger.balance(session, ledger.EXTERNAL) == -100
    assert session.scalar(select(func.sum(LedgerEntry.amount))) == 0
    assert session.scalar(select(func.count(LedgerEntry.id))) == 4


def test_retry_with_same_key_does_not_move_twice(session):
    for _ in range(3):
        ledger.transfer(session, ledger.EXTERNAL, "human_pool", 50, "same-key")
    session.commit()
    assert ledger.balance(session, "human_pool") == 50
    assert session.scalar(select(func.count(Transfer.id))) == 1


def test_cannot_overspend(session):
    ledger.transfer(session, ledger.EXTERNAL, "agent_wallet:1", 5, "fund")
    with pytest.raises(ledger.InsufficientFunds):
        ledger.transfer(session, "agent_wallet:1", "vendor_payable:1", 6, "spend")
    assert ledger.balance(session, "agent_wallet:1") == 5


@pytest.mark.parametrize("amount", [0, -3])
def test_rejects_non_positive_amounts(session, amount):
    with pytest.raises(ValueError):
        ledger.transfer(session, ledger.EXTERNAL, "human_pool", amount, f"bad{amount}")


def test_rejects_self_transfer(session):
    with pytest.raises(ValueError):
        ledger.transfer(session, "human_pool", "human_pool", 1, "self")


def test_balance_matches_sum_of_entries(session):
    ledger.transfer(session, ledger.EXTERNAL, "a", 20, "k1")
    ledger.transfer(session, "a", "b", 7, "k2")
    ledger.transfer(session, "b", "a", 2, "k3")
    session.commit()
    for name in ["a", "b"]:
        acct = ledger.get_or_create_account(session, name)
        total = session.scalar(select(func.sum(LedgerEntry.amount)).where(LedgerEntry.account_id == acct.id))
        assert total == acct.balance
