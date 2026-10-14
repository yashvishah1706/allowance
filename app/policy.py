"""Policy engine: decides whether an agent's purchase may go ahead.

This is deliberately pure Python with no database or AI inside it.
The AI only *requests* a purchase; this code decides. Every rule here
is enforced regardless of what the model says.
"""

from dataclasses import dataclass, field

# Numeric limits per trust tier (credits). Tune during the build.
TIERS = {
    "new":     {"max_per_purchase": 2,  "period_cap": 20,  "ask_above": 0},   # asks before every purchase
    "proven":  {"max_per_purchase": 5,  "period_cap": 50,  "ask_above": 3},
    "trusted": {"max_per_purchase": 15, "period_cap": 150, "ask_above": 10},
}

ALLOW, ASK, BLOCK = "allow", "ask", "block"


@dataclass
class PurchaseRequest:
    vendor: str
    category: str
    price: int
    justification: str = ""
    task_id: int | None = None


@dataclass
class AgentContext:
    trust_level: str
    balance: int
    spent_this_period: int
    frozen: bool = False
    allowed_categories: list[str] = field(default_factory=list)
    blocked_vendors: list[str] = field(default_factory=list)
    min_reserve_pct: int = 20
    require_justification: bool = True
    reserve_base: int | None = None  # balance the reserve % is measured against; defaults to balance


@dataclass
class Decision:
    status: str          # allow | ask | block
    check: str | None    # which check decided, None when fully allowed
    reason: str

    @property
    def allowed(self) -> bool:
        return self.status == ALLOW


def evaluate(req: PurchaseRequest, ctx: AgentContext, human_approved: bool = False) -> Decision:
    """Run the 8 checks in order. The first failing check decides.

    human_approved=True means a person already approved this exact purchase,
    so check 8 (ask the human) is skipped. Checks 1-7 still apply.
    """
    if ctx.trust_level not in TIERS:
        return Decision(BLOCK, "trust_level", f"unknown trust level '{ctx.trust_level}'")
    tier = TIERS[ctx.trust_level]

    if req.price <= 0:
        return Decision(BLOCK, "price", "price must be positive")

    # 1. Wallet frozen
    if ctx.frozen:
        return Decision(BLOCK, "frozen", "wallet is frozen by the owner")

    # 2. Vendor allowed
    if req.vendor in ctx.blocked_vendors:
        return Decision(BLOCK, "vendor", f"vendor '{req.vendor}' is blocked")

    # 3. Category allowed
    if req.category not in ctx.allowed_categories:
        return Decision(BLOCK, "category", f"category '{req.category}' is not allowed")

    # 4. Max per purchase
    if req.price > tier["max_per_purchase"]:
        return Decision(BLOCK, "max_per_purchase",
                        f"{req.price} credits is over the {tier['max_per_purchase']}-credit limit per purchase "
                        f"for a {ctx.trust_level} agent")

    # 5. Period cap
    if ctx.spent_this_period + req.price > tier["period_cap"]:
        return Decision(BLOCK, "period_cap",
                        f"would bring spending this period to {ctx.spent_this_period + req.price}, "
                        f"over the cap of {tier['period_cap']}")

    # 6. Reserve: keep a share of the wallet unspent (also covers insufficient balance)
    if req.price > ctx.balance:
        return Decision(BLOCK, "balance", f"wallet has {ctx.balance} credits, needs {req.price}")
    base = ctx.balance if ctx.reserve_base is None else ctx.reserve_base
    reserve = -(-base * ctx.min_reserve_pct // 100)  # ceiling
    if ctx.balance - req.price < reserve:
        return Decision(BLOCK, "reserve",
                        f"must keep at least {reserve} credits ({ctx.min_reserve_pct}%) in reserve")

    # 7. Justification tied to the task
    if ctx.require_justification:
        if len(req.justification.strip()) < 15:
            return Decision(BLOCK, "justification", "explain how this purchase helps the current task")
        if req.task_id is None:
            return Decision(BLOCK, "justification", "purchase must be tied to a task")

    # 8. Ask the human above the threshold
    if not human_approved and req.price > tier["ask_above"]:
        return Decision(ASK, "ask_human",
                        f"{req.price} credits is above the {tier['ask_above']}-credit auto-approve limit; "
                        f"waiting for the owner")

    return Decision(ALLOW, None, "allowed")


# ---------- trust levels ----------

def next_trust_level(level: str, approved_streak: int, approved_total: int, avg_rating: float, rejected: bool) -> str:
    """New -> Proven after 3 approvals in a row with avg >= 4.
    Proven -> Trusted after 10 approvals total with avg >= 4.5.
    One rejection drops one level."""
    order = ["new", "proven", "trusted"]
    i = order.index(level)
    if rejected:
        return order[max(0, i - 1)]
    if level == "new" and approved_streak >= 3 and avg_rating >= 4:
        return "proven"
    if level == "proven" and approved_total >= 10 and avg_rating >= 4.5:
        return "trusted"
    return level
