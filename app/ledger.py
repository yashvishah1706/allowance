"""Double-entry credit ledger.

Rules:
- Credits only move through transfer(); balances are never edited directly.
- Each transfer writes two entries that sum to zero.
- Both accounts are locked (SELECT ... FOR UPDATE on Postgres) in a fixed order,
  so parallel transfers cannot overspend or deadlock.
- An idempotency key makes a retried transfer a no-op instead of a double move.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Account, LedgerEntry, Transfer

EXTERNAL = "external:paypal"


class InsufficientFunds(Exception):
    pass


def get_or_create_account(session: Session, name: str, allow_negative: bool = False) -> Account:
    """Safe under concurrency: if two requests create the same account at once,
    the loser's insert is rolled back to a savepoint and it reads the winner's row."""
    acct = session.scalar(select(Account).where(Account.name == name))
    if acct is not None:
        return acct
    try:
        with session.begin_nested():
            acct = Account(name=name, allow_negative=allow_negative, balance=0)
            session.add(acct)
            session.flush()
        return acct
    except IntegrityError:
        return session.scalar(select(Account).where(Account.name == name))


def balance(session: Session, name: str) -> int:
    acct = session.scalar(select(Account).where(Account.name == name))
    return acct.balance if acct else 0


def transfer(session: Session, src: str, dst: str, amount: int, idempotency_key: str, memo: str = "") -> Transfer:
    """Move `amount` credits from src to dst. Caller commits the session."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    if src == dst:
        raise ValueError("source and destination must differ")

    existing = session.scalar(select(Transfer).where(Transfer.idempotency_key == idempotency_key))
    if existing is not None:
        return existing  # retry of a transfer that already happened

    get_or_create_account(session, src, allow_negative=src.startswith("external:"))
    get_or_create_account(session, dst, allow_negative=dst.startswith("external:"))

    # Lock both rows in name order to avoid deadlocks between opposite transfers.
    locked = {
        a.name: a
        for a in session.scalars(
            select(Account).where(Account.name.in_([src, dst])).order_by(Account.name).with_for_update()
        )
    }
    source, dest = locked[src], locked[dst]

    if not source.allow_negative and source.balance < amount:
        raise InsufficientFunds(f"{src} has {source.balance} credits, needs {amount}")

    t = Transfer(idempotency_key=idempotency_key, amount=amount, memo=memo)
    session.add(t)
    session.flush()
    session.add_all([
        LedgerEntry(transfer_id=t.id, account_id=source.id, amount=-amount),
        LedgerEntry(transfer_id=t.id, account_id=dest.id, amount=amount),
    ])
    source.balance -= amount
    dest.balance += amount
    session.flush()
    return t
