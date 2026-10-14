"""Database tables.

Money is tracked as integer credits in a double-entry ledger:
every transfer writes two LedgerEntry rows (one -amount, one +amount)
that sum to zero, and Account.balance is a cached sum of its entries.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now():
    return datetime.now(timezone.utc)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    # e.g. "external:paypal", "human_pool", "escrow:task:7", "agent_wallet:1", "vendor_payable:2"
    name: Mapped[str] = mapped_column(String(80), unique=True)
    # External accounts represent money outside the system and may go negative.
    allow_negative: Mapped[bool] = mapped_column(default=False)
    balance: Mapped[int] = mapped_column(Integer, default=0)


class Transfer(Base):
    __tablename__ = "transfers"
    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    amount: Mapped[int] = mapped_column(Integer)
    memo: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (CheckConstraint("amount > 0", name="positive_amount"),)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    transfer_id: Mapped[int] = mapped_column(ForeignKey("transfers.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    amount: Mapped[int] = mapped_column(Integer)  # negative = debit, positive = credit
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    trust_level: Mapped[str] = mapped_column(String(20), default="new")  # new | proven | trusted
    approved_streak: Mapped[int] = mapped_column(Integer, default=0)
    approved_total: Mapped[int] = mapped_column(Integer, default=0)
    rating_sum: Mapped[int] = mapped_column(Integer, default=0)
    frozen: Mapped[bool] = mapped_column(default=False)


class Policy(Base):
    """Human-editable rules for one agent. Numeric limits come from the trust tier."""
    __tablename__ = "policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), unique=True)
    allowed_categories: Mapped[list] = mapped_column(JSON, default=lambda: ["data_api", "search", "enrichment"])
    blocked_vendors: Mapped[list] = mapped_column(JSON, default=list)
    min_reserve_pct: Mapped[int] = mapped_column(Integer, default=20)
    require_justification: Mapped[bool] = mapped_column(default=True)


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    paypal_email: Mapped[str] = mapped_column(String(120), default="")


class Tool(Base):
    __tablename__ = "tools"
    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    name: Mapped[str] = mapped_column(String(80), unique=True)
    category: Mapped[str] = mapped_column(String(40))
    price_credits: Mapped[int] = mapped_column(Integer)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    title: Mapped[str] = mapped_column(String(200))
    reward_credits: Mapped[int] = mapped_column(Integer)
    # awaiting_funding -> funded -> submitted -> approved | rejected
    status: Mapped[str] = mapped_column(String(30), default="awaiting_funding")
    paypal_order_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    paypal_authorization_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    approve_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_report: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expenses_reimbursed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (CheckConstraint("reward_credits > 0", name="positive_reward"),)


class Purchase(Base):
    __tablename__ = "purchases"
    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    tool_id: Mapped[int] = mapped_column(ForeignKey("tools.id"))
    price_credits: Mapped[int] = mapped_column(Integer)
    justification: Mapped[str] = mapped_column(Text, default="")
    # completed | pending_approval | blocked | declined
    status: Mapped[str] = mapped_column(String(30))
    decision_check: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("agent_id", "idempotency_key", name="uq_purchase_idem"),)


class TopUp(Base):
    """Owner adds money to the allowance pool (PayPal order with intent CAPTURE)."""
    __tablename__ = "topups"
    id: Mapped[int] = mapped_column(primary_key=True)
    credits: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="awaiting_payment")  # awaiting_payment | completed
    paypal_order_id: Mapped[str] = mapped_column(String(60))
    approve_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PayPalEvent(Base):
    """Every webhook event id we have processed, so a redelivered event is ignored."""
    __tablename__ = "paypal_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(80), unique=True)
    event_type: Mapped[str] = mapped_column(String(80))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
