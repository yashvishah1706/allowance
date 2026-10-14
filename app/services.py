"""Business logic: top-ups, tasks (escrow), reviews, purchases, trust.

Each function takes a SQLAlchemy session and a PayPal client and commits on success.
API routes in main.py are thin wrappers around these.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ledger
from .config import credits_to_usd
from .models import Agent, Policy, Purchase, Task, Tool, TopUp, Vendor
from .policy import ALLOW, ASK, AgentContext, PurchaseRequest, evaluate, next_trust_level

HUMAN_POOL = "human_pool"
PERIOD = timedelta(days=7)


class NotFound(Exception):
    pass


class InvalidState(Exception):
    pass


def wallet(agent_id: int) -> str:
    return f"agent_wallet:{agent_id}"


def escrow(task_id: int) -> str:
    return f"escrow:task:{task_id}"


def vendor_payable(vendor_id: int) -> str:
    return f"vendor_payable:{vendor_id}"


def _get(session, model, id_):
    obj = session.get(model, id_)
    if obj is None:
        raise NotFound(f"{model.__name__} {id_} not found")
    return obj


# ---------- agents ----------

def create_agent(session: Session, name: str) -> Agent:
    agent = Agent(name=name)
    session.add(agent)
    session.flush()
    session.add(Policy(agent_id=agent.id))
    ledger.get_or_create_account(session, wallet(agent.id))
    session.commit()
    return agent


def grant_allowance(session: Session, agent_id: int, credits: int, idem: str) -> int:
    """Owner moves credits from the pool into an agent's wallet (a starter allowance)."""
    _get(session, Agent, agent_id)
    ledger.transfer(session, HUMAN_POOL, wallet(agent_id), credits, f"grant:{idem}", "starter allowance")
    session.commit()
    return ledger.balance(session, wallet(agent_id))


# ---------- top-ups ----------

def start_topup(session: Session, paypal, credits: int) -> TopUp:
    t = TopUp(credits=credits, paypal_order_id="pending")
    session.add(t)
    session.flush()
    order = paypal.create_capture_order(credits_to_usd(credits), f"Allowance top-up: {credits} credits",
                                        f"topup-{t.id}", f"topup-create-{t.id}")
    t.paypal_order_id, t.approve_url = order["order_id"], order["approve_url"]
    session.commit()
    return t


def complete_topup(session: Session, paypal, topup_id: int) -> TopUp:
    t = _get(session, TopUp, topup_id)
    if t.status == "completed":
        return t
    paypal.capture_order(t.paypal_order_id, f"topup-capture-{t.id}")
    ledger.transfer(session, ledger.EXTERNAL, HUMAN_POOL, t.credits, f"topup:{t.id}", "top-up captured")
    t.status = "completed"
    session.commit()
    return t


# ---------- tasks and escrow ----------

def post_task(session: Session, paypal, agent_id: int, title: str, reward_credits: int) -> Task:
    _get(session, Agent, agent_id)
    task = Task(agent_id=agent_id, title=title, reward_credits=reward_credits)
    session.add(task)
    session.flush()
    order = paypal.create_authorize_order(credits_to_usd(reward_credits), f"Allowance task reward: {title}",
                                          f"task-{task.id}", f"task-create-{task.id}")
    task.paypal_order_id, task.approve_url = order["order_id"], order["approve_url"]
    session.commit()
    return task


def fund_task(session: Session, paypal, task_id: int) -> Task:
    """Call after the owner approved the PayPal order: authorize = hold the reward."""
    task = _get(session, Task, task_id)
    if task.status != "awaiting_funding":
        return task  # already funded; retry is a no-op
    auth = paypal.authorize_order(task.paypal_order_id, f"task-authorize-{task.id}")
    task.paypal_authorization_id = auth["authorization_id"]
    ledger.transfer(session, ledger.EXTERNAL, escrow(task.id), task.reward_credits,
                    f"escrow-hold:{task.id}", "reward authorized and held")
    task.status = "funded"
    session.commit()
    return task


def submit_work(session: Session, task_id: int, preview: str, full_report: str) -> Task:
    task = _get(session, Task, task_id)
    if task.status not in ("funded", "submitted"):
        raise InvalidState(f"task is {task.status}; it must be funded before work is submitted")
    task.preview, task.full_report, task.status = preview, full_report, "submitted"
    session.commit()
    return task


def task_expenses(session: Session, task: Task) -> int:
    rows = session.scalars(select(Purchase).where(Purchase.task_id == task.id, Purchase.status == "completed"))
    return sum(p.price_credits for p in rows)


def _update_trust(agent: Agent, rejected: bool, rating: int | None):
    if rejected:
        agent.approved_streak = 0
    else:
        agent.approved_streak += 1
        agent.approved_total += 1
        agent.rating_sum += rating or 0
    avg = agent.rating_sum / agent.approved_total if agent.approved_total else 0
    agent.trust_level = next_trust_level(agent.trust_level, agent.approved_streak, agent.approved_total, avg, rejected)


def approve_task(session: Session, paypal, task_id: int, rating: int) -> Task:
    task = _get(session, Task, task_id)
    if task.status == "approved":
        return task
    if task.status != "submitted":
        raise InvalidState(f"task is {task.status}; only submitted work can be approved")
    if not 1 <= rating <= 5:
        raise ValueError("rating must be 1-5")
    paypal.capture(task.paypal_authorization_id, None, f"task-capture-{task.id}")
    ledger.transfer(session, escrow(task.id), wallet(task.agent_id), task.reward_credits,
                    f"escrow-pay:{task.id}", "reward paid on approval")
    task.status, task.rating = "approved", rating
    _update_trust(_get(session, Agent, task.agent_id), rejected=False, rating=rating)
    session.commit()
    return task


def reject_task(session: Session, paypal, task_id: int, reason: str) -> Task:
    """No fee is paid, but the agent's tool expenses for this task are covered:
    PayPal captures only the expenses (final_capture releases the rest), or voids if none."""
    task = _get(session, Task, task_id)
    if task.status == "rejected":
        return task
    if task.status != "submitted":
        raise InvalidState(f"task is {task.status}; only submitted work can be rejected")
    if len(reason.strip()) < 5:
        raise ValueError("give a reason for rejecting")
    reimburse = min(task_expenses(session, task), task.reward_credits)
    if reimburse > 0:
        paypal.capture(task.paypal_authorization_id, credits_to_usd(reimburse), f"task-expenses-{task.id}")
        ledger.transfer(session, escrow(task.id), wallet(task.agent_id), reimburse,
                        f"escrow-expenses:{task.id}", "tool expenses covered on rejection")
    else:
        paypal.void(task.paypal_authorization_id, f"task-void-{task.id}")
    released = task.reward_credits - reimburse
    if released > 0:
        ledger.transfer(session, escrow(task.id), ledger.EXTERNAL, released,
                        f"escrow-release:{task.id}", "hold released to owner")
    task.status, task.review_reason, task.expenses_reimbursed = "rejected", reason, reimburse
    _update_trust(_get(session, Agent, task.agent_id), rejected=True, rating=None)
    session.commit()
    return task


# ---------- purchases ----------

def _context(session: Session, agent: Agent) -> AgentContext:
    pol = session.scalar(select(Policy).where(Policy.agent_id == agent.id))
    cutoff = datetime.now(timezone.utc) - PERIOD
    spent = 0
    for p in session.scalars(select(Purchase).where(Purchase.agent_id == agent.id, Purchase.status == "completed")):
        created = p.created_at if p.created_at.tzinfo else p.created_at.replace(tzinfo=timezone.utc)
        if created >= cutoff:
            spent += p.price_credits
    return AgentContext(
        trust_level=agent.trust_level, balance=ledger.balance(session, wallet(agent.id)),
        spent_this_period=spent, frozen=agent.frozen,
        allowed_categories=list(pol.allowed_categories), blocked_vendors=list(pol.blocked_vendors),
        min_reserve_pct=pol.min_reserve_pct, require_justification=pol.require_justification,
    )


def _execute(session: Session, purchase: Purchase, tool: Tool):
    ledger.transfer(session, wallet(purchase.agent_id), vendor_payable(tool.vendor_id), purchase.price_credits,
                    f"purchase:{purchase.id}", f"{tool.name} for task {purchase.task_id}")
    purchase.status = "completed"


def request_purchase(session: Session, agent_id: int, tool_id: int, task_id: int | None,
                     justification: str, idempotency_key: str) -> Purchase:
    existing = session.scalar(select(Purchase).where(Purchase.agent_id == agent_id,
                                                     Purchase.idempotency_key == idempotency_key))
    if existing is not None:
        return existing

    agent = _get(session, Agent, agent_id)
    tool = _get(session, Tool, tool_id)
    vendor = _get(session, Vendor, tool.vendor_id)
    if task_id is not None:
        task = _get(session, Task, task_id)
        if task.agent_id != agent_id or task.status not in ("funded", "submitted"):
            raise InvalidState("purchases must belong to one of this agent's active tasks")

    req = PurchaseRequest(vendor=vendor.name, category=tool.category, price=tool.price_credits,
                          justification=justification, task_id=task_id)
    decision = evaluate(req, _context(session, agent))
    purchase = Purchase(idempotency_key=idempotency_key, agent_id=agent_id, task_id=task_id, tool_id=tool_id,
                        price_credits=tool.price_credits, justification=justification,
                        status={ALLOW: "completed", ASK: "pending_approval"}.get(decision.status, "blocked"),
                        decision_check=decision.check, decision_reason=decision.reason)
    session.add(purchase)
    session.flush()
    if decision.status == ALLOW:
        _execute(session, purchase, tool)
    session.commit()
    return purchase


def review_purchase(session: Session, purchase_id: int, approve: bool) -> Purchase:
    """Owner answers a pending purchase. Checks 1-7 run again; only check 8 is satisfied."""
    purchase = _get(session, Purchase, purchase_id)
    if purchase.status != "pending_approval":
        return purchase
    if not approve:
        purchase.status, purchase.decision_reason = "declined", "owner declined"
        session.commit()
        return purchase
    agent = _get(session, Agent, purchase.agent_id)
    tool = _get(session, Tool, purchase.tool_id)
    vendor = _get(session, Vendor, tool.vendor_id)
    req = PurchaseRequest(vendor=vendor.name, category=tool.category, price=purchase.price_credits,
                          justification=purchase.justification, task_id=purchase.task_id)
    decision = evaluate(req, _context(session, agent), human_approved=True)
    purchase.decision_check, purchase.decision_reason = decision.check, decision.reason
    if decision.status == ALLOW:
        _execute(session, purchase, tool)
    else:
        purchase.status = "blocked"
    session.commit()
    return purchase
