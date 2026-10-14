"""Allowance API.

Run:  uvicorn app.main:app --reload
Docs: http://localhost:8000/docs
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ledger, services
from .config import settings
from .db import Base, engine, get_session
from .models import Agent, Purchase, Task, Tool, Vendor
from .paypal import FakePayPal, PayPalClient, PayPalError
from .policy import TIERS

@asynccontextmanager
async def lifespan(_app):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Allowance", description="AI agents earn their budget, and their freedom.", lifespan=lifespan)
_paypal = FakePayPal() if settings.paypal_mode == "fake" else PayPalClient()


def get_paypal():
    return _paypal


def _handle(fn):
    try:
        return fn()
    except services.NotFound as e:
        raise HTTPException(404, str(e))
    except (services.InvalidState, ValueError) as e:
        raise HTTPException(409, str(e))
    except ledger.InsufficientFunds as e:
        raise HTTPException(409, str(e))
    except PayPalError as e:
        raise HTTPException(502, str(e))


# ---------- request bodies ----------

class AgentIn(BaseModel):
    name: str


class CreditsIn(BaseModel):
    credits: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=100)


class TopUpIn(BaseModel):
    credits: int = Field(gt=0, le=10_000)


class TaskIn(BaseModel):
    agent_id: int
    title: str = Field(min_length=3)
    reward_credits: int = Field(gt=0, le=10_000)


class SubmitIn(BaseModel):
    preview: str
    full_report: str


class ApproveIn(BaseModel):
    rating: int = Field(ge=1, le=5)


class RejectIn(BaseModel):
    reason: str = Field(min_length=5)


class PurchaseIn(BaseModel):
    tool_id: int
    task_id: int | None = None
    justification: str = ""
    idempotency_key: str = Field(min_length=1, max_length=100)


class ReviewIn(BaseModel):
    approve: bool


# ---------- helpers ----------

def agent_view(session: Session, a: Agent) -> dict:
    tier = TIERS[a.trust_level]
    return {
        "id": a.id, "name": a.name, "trust_level": a.trust_level, "frozen": a.frozen,
        "wallet_credits": ledger.balance(session, services.wallet(a.id)),
        "approved_streak": a.approved_streak, "approved_total": a.approved_total,
        "limits": tier,
    }


def task_view(t: Task, include_report: bool = False) -> dict:
    out = {
        "id": t.id, "agent_id": t.agent_id, "title": t.title, "reward_credits": t.reward_credits,
        "status": t.status, "approve_url": t.approve_url, "preview": t.preview, "rating": t.rating,
        "review_reason": t.review_reason, "expenses_reimbursed": t.expenses_reimbursed,
    }
    if include_report:
        out["full_report"] = t.full_report
    return out


def purchase_view(p: Purchase) -> dict:
    return {
        "id": p.id, "agent_id": p.agent_id, "task_id": p.task_id, "tool_id": p.tool_id,
        "price_credits": p.price_credits, "status": p.status, "check": p.decision_check,
        "reason": p.decision_reason, "justification": p.justification,
    }


# ---------- routes ----------

@app.get("/health")
def health():
    return {"ok": True, "paypal_mode": settings.paypal_mode}


@app.post("/agents")
def create_agent(body: AgentIn, s: Session = Depends(get_session)):
    return agent_view(s, services.create_agent(s, body.name))


@app.get("/agents/{agent_id}")
def get_agent(agent_id: int, s: Session = Depends(get_session)):
    a = s.get(Agent, agent_id)
    if a is None:
        raise HTTPException(404, "agent not found")
    return agent_view(s, a)


@app.post("/agents/{agent_id}/grant")
def grant(agent_id: int, body: CreditsIn, s: Session = Depends(get_session)):
    return {"wallet_credits": _handle(lambda: services.grant_allowance(s, agent_id, body.credits, body.idempotency_key))}


@app.post("/topups")
def start_topup(body: TopUpIn, s: Session = Depends(get_session), pp=Depends(get_paypal)):
    t = _handle(lambda: services.start_topup(s, pp, body.credits))
    return {"id": t.id, "credits": t.credits, "status": t.status, "approve_url": t.approve_url}


@app.post("/topups/{topup_id}/complete")
def complete_topup(topup_id: int, s: Session = Depends(get_session), pp=Depends(get_paypal)):
    t = _handle(lambda: services.complete_topup(s, pp, topup_id))
    return {"id": t.id, "status": t.status, "pool_credits": ledger.balance(s, services.HUMAN_POOL)}


@app.post("/tasks")
def post_task(body: TaskIn, s: Session = Depends(get_session), pp=Depends(get_paypal)):
    return task_view(_handle(lambda: services.post_task(s, pp, body.agent_id, body.title, body.reward_credits)))


@app.post("/tasks/{task_id}/fund")
def fund_task(task_id: int, s: Session = Depends(get_session), pp=Depends(get_paypal)):
    return task_view(_handle(lambda: services.fund_task(s, pp, task_id)))


@app.post("/tasks/{task_id}/submit")
def submit(task_id: int, body: SubmitIn, s: Session = Depends(get_session)):
    return task_view(_handle(lambda: services.submit_work(s, task_id, body.preview, body.full_report)))


@app.get("/tasks/{task_id}")
def get_task(task_id: int, s: Session = Depends(get_session)):
    t = s.get(Task, task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    return task_view(t)


@app.get("/tasks/{task_id}/report")
def get_report(task_id: int, s: Session = Depends(get_session)):
    """The full report unlocks only after the reward is captured."""
    t = s.get(Task, task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    if t.status != "approved":
        raise HTTPException(402, "Payment required: approve the work to unlock the full report")
    return task_view(t, include_report=True)


@app.post("/tasks/{task_id}/approve")
def approve(task_id: int, body: ApproveIn, s: Session = Depends(get_session), pp=Depends(get_paypal)):
    return task_view(_handle(lambda: services.approve_task(s, pp, task_id, body.rating)), include_report=True)


@app.post("/tasks/{task_id}/reject")
def reject(task_id: int, body: RejectIn, s: Session = Depends(get_session), pp=Depends(get_paypal)):
    return task_view(_handle(lambda: services.reject_task(s, pp, task_id, body.reason)))


@app.get("/tools")
def list_tools(s: Session = Depends(get_session)):
    return [{"id": t.id, "name": t.name, "category": t.category, "price_credits": t.price_credits}
            for t in s.scalars(select(Tool))]


@app.post("/agents/{agent_id}/purchases")
def purchase(agent_id: int, body: PurchaseIn, s: Session = Depends(get_session)):
    p = _handle(lambda: services.request_purchase(s, agent_id, body.tool_id, body.task_id,
                                                   body.justification, body.idempotency_key))
    return purchase_view(p)


@app.post("/purchases/{purchase_id}/review")
def review(purchase_id: int, body: ReviewIn, s: Session = Depends(get_session)):
    return purchase_view(_handle(lambda: services.review_purchase(s, purchase_id, body.approve)))


@app.post("/seed")
def seed(s: Session = Depends(get_session)):
    """Create the three demo tools if they don't exist."""
    demo = [("Company Info", "data_api", 2), ("Funding Data", "data_api", 4), ("News Sentiment", "enrichment", 12)]
    for name, cat, price in demo:
        if s.scalar(select(Tool).where(Tool.name == name)) is None:
            v = Vendor(name=f"{name} Inc")
            s.add(v)
            s.flush()
            s.add(Tool(vendor_id=v.id, name=name, category=cat, price_credits=price))
    s.commit()
    return list_tools(s)
