"""Unit tests for the policy engine: every check, in order, plus trust levels."""

import pytest

from app.policy import ALLOW, ASK, BLOCK, AgentContext, PurchaseRequest, evaluate, next_trust_level

GOOD_REASON = "Last report lacked funding data for these companies"


def ctx(**kw):
    base = dict(trust_level="proven", balance=40, spent_this_period=0,
                allowed_categories=["data_api", "search", "enrichment"], blocked_vendors=[],
                min_reserve_pct=20, require_justification=True)
    base.update(kw)
    return AgentContext(**base)


def req(**kw):
    base = dict(vendor="Funding Data Inc", category="data_api", price=3, justification=GOOD_REASON, task_id=1)
    base.update(kw)
    return PurchaseRequest(**base)


def test_valid_purchase_is_allowed():
    d = evaluate(req(), ctx())
    assert d.status == ALLOW and d.allowed and d.check is None


@pytest.mark.parametrize("price", [0, -5])
def test_non_positive_price_blocked(price):
    assert evaluate(req(price=price), ctx()).check == "price"


def test_unknown_trust_level_blocked():
    assert evaluate(req(), ctx(trust_level="godmode")).status == BLOCK


def test_frozen_wallet_blocked():
    d = evaluate(req(), ctx(frozen=True))
    assert d.status == BLOCK and d.check == "frozen"


def test_blocked_vendor():
    d = evaluate(req(), ctx(blocked_vendors=["Funding Data Inc"]))
    assert d.status == BLOCK and d.check == "vendor"


def test_disallowed_category():
    d = evaluate(req(category="gift_cards"), ctx())
    assert d.status == BLOCK and d.check == "category"


@pytest.mark.parametrize("level,limit", [("new", 2), ("proven", 5), ("trusted", 15)])
def test_max_per_purchase_by_tier(level, limit):
    big = ctx(trust_level=level, balance=1000)
    assert evaluate(req(price=limit + 1), big).check == "max_per_purchase"
    assert evaluate(req(price=limit), big, human_approved=True).status == ALLOW


def test_max_per_purchase_reason_names_limit():
    d = evaluate(req(price=12), ctx())
    assert "5-credit limit" in d.reason


def test_period_cap():
    d = evaluate(req(price=4), ctx(spent_this_period=48))
    assert d.status == BLOCK and d.check == "period_cap"


def test_period_cap_exact_boundary_allowed():
    assert evaluate(req(price=2), ctx(spent_this_period=48)).status == ALLOW


def test_insufficient_balance():
    d = evaluate(req(price=4), ctx(balance=3))
    assert d.check == "balance"


def test_reserve_rule():
    # balance 10, reserve 20% = 2, buying 5 leaves 5 -> ok; buying 5 from balance 6 leaves 1 -> blocked
    assert evaluate(req(price=5), ctx(balance=10), human_approved=True).status == ALLOW
    d = evaluate(req(price=5), ctx(balance=6), human_approved=True)
    assert d.check == "reserve"


def test_reserve_zero_pct_allows_spending_everything():
    assert evaluate(req(price=5), ctx(balance=5, min_reserve_pct=0), human_approved=True).status == ALLOW


@pytest.mark.parametrize("why", ["", "   ", "need it"])
def test_missing_or_vague_justification(why):
    d = evaluate(req(justification=why), ctx())
    assert d.check == "justification"


def test_purchase_must_be_tied_to_task():
    d = evaluate(req(task_id=None), ctx())
    assert d.check == "justification"


def test_justification_optional_when_policy_allows():
    assert evaluate(req(justification="", task_id=None), ctx(require_justification=False)).status == ALLOW


def test_new_agent_must_ask_before_every_purchase():
    d = evaluate(req(price=1), ctx(trust_level="new"))
    assert d.status == ASK and d.check == "ask_human"


def test_proven_agent_asks_above_threshold():
    assert evaluate(req(price=3), ctx()).status == ALLOW
    assert evaluate(req(price=4), ctx()).status == ASK


def test_human_approval_skips_only_the_ask_check():
    assert evaluate(req(price=4), ctx(), human_approved=True).status == ALLOW
    # approval does not override a hard limit
    assert evaluate(req(price=12), ctx(), human_approved=True).status == BLOCK


def test_checks_run_in_order_first_failure_wins():
    d = evaluate(req(price=99, category="gift_cards"), ctx(frozen=True))
    assert d.check == "frozen"


# ---------- trust ----------

def test_new_becomes_proven_after_three_good_approvals():
    assert next_trust_level("new", 2, 2, 5, False) == "new"
    assert next_trust_level("new", 3, 3, 4.3, False) == "proven"


def test_low_ratings_do_not_promote():
    assert next_trust_level("new", 3, 3, 3.5, False) == "new"


def test_proven_becomes_trusted():
    assert next_trust_level("proven", 4, 10, 4.6, False) == "trusted"
    assert next_trust_level("proven", 4, 10, 4.4, False) == "proven"


@pytest.mark.parametrize("level,expected", [("trusted", "proven"), ("proven", "new"), ("new", "new")])
def test_rejection_drops_one_level(level, expected):
    assert next_trust_level(level, 0, 5, 5, True) == expected
