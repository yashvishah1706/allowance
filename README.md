# Allowance

**AI agents earn their budget, and their freedom.**

As kids, we didn't get freedom handed to us; we earned it. Today people give AI agents either no access to money (so they get stuck on any paid tool) or a raw card with no limits. Allowance sits in between: agents earn spending power by doing work a person approves, spend it on tools they decide will help, and gain or lose freedom based on how well they do.

> The AI decides what to buy. The code decides whether it's allowed.

Built for the PayPal AI Hackathon. Runs entirely on the PayPal **sandbox**; no real money moves.

## How it works

1. **Post a task with a reward.** PayPal creates an order with intent `AUTHORIZE`. Once you approve it, the reward is **held** (authorized), not charged.
2. **The agent works.** When it needs a paid tool, it *requests* a purchase. A policy engine runs 8 checks before any credit moves.
3. **You see a preview, not the full report.** The full report returns `402 Payment Required` until you approve.
4. **Approve:** PayPal **captures** the reward, the agent's wallet is credited, the report unlocks, and its trust can rise.
5. **Reject:** you pay no fee. PayPal captures only the agent's tool expenses for that task (`final_capture` releases the rest), or **voids** the hold if there were none. Trust drops one level.

### Policy engine (`app/policy.py`)

Checks run in order; the first failure decides:

1. Wallet frozen
2. Vendor on the block list
3. Category not allowed
4. Price over the per-purchase limit for the agent's trust tier
5. Spending this period would exceed the cap
6. Balance would drop below the reserve (default 20%)
7. No justification, or not tied to a task
8. Price above the auto-approve threshold → wait for the owner

| Trust tier | Per purchase | Period cap | Asks owner above |
|---|---|---|---|
| New | 2 credits | 20 | every purchase |
| Proven (3 approvals in a row, avg ≥ 4) | 5 | 50 | 3 |
| Trusted (10 approvals, avg ≥ 4.5) | 15 | 150 | 10 |

One rejection drops the agent one tier. 1 credit = $0.10.

### Money safety

- **Double-entry ledger** (`app/ledger.py`): every transfer writes two entries that sum to zero; balances are never edited directly.
- **Row locks** (`SELECT … FOR UPDATE`, in a fixed order) so parallel purchases cannot overspend or deadlock.
- **Idempotency keys** on every transfer, purchase and PayPal call (`PayPal-Request-Id`), so retries never charge twice.

## Run it

Requirements: Python 3.11+, and Docker (for Postgres) or a local Postgres.

```bash
git clone https://github.com/yashvishah1706/allowance.git
cd allowance
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in your sandbox credentials
docker compose up -d db
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs for the interactive API.

**No PayPal account yet?** Set `PAYPAL_MODE=fake` in `.env` to run the whole flow offline.

### PayPal sandbox setup

1. Log in at https://developer.paypal.com → **Apps & Credentials** → toggle **Sandbox** → **Create App**.
2. Copy the Client ID and Secret into `.env`.
3. Under **Testing Tools → Sandbox Accounts**, note the **Personal** account's email and password; you'll use it to approve payments.
4. Quick check: `python scripts/escrow_demo.py approve`, then `python scripts/escrow_demo.py reject`. Both should print `PASS`.

### Walk through the API

```text
POST /seed                                  create the 3 demo tools
POST /agents            {"name": "research-agent"}
POST /topups            {"credits": 200}    → open approve_url as the sandbox buyer
POST /topups/1/complete                     → credits land in the owner's pool
POST /agents/1/grant    {"credits": 5, "idempotency_key": "starter"}
POST /tasks             {"agent_id": 1, "title": "Research 5 fintech startups", "reward_credits": 30}
                                            → open approve_url as the sandbox buyer
POST /tasks/1/fund                          → reward authorized and held
POST /agents/1/purchases {"tool_id": 1, "task_id": 1, "justification": "...", "idempotency_key": "p1"}
POST /purchases/1/review {"approve": true}
POST /tasks/1/submit    {"preview": "...", "full_report": "..."}
GET  /tasks/1/report                        → 402 until approved
POST /tasks/1/approve   {"rating": 5}       or  POST /tasks/1/reject {"reason": "..."}
```

## Tests

```bash
pytest                                       # policy, ledger and end-to-end flow (SQLite + fake PayPal)
DATABASE_URL=postgresql+psycopg://allowance:allowance@localhost:5432/allowance \
  pytest tests/test_concurrency.py           # 50 parallel purchases, zero overspend (needs Postgres)
```

## Roadmap

- [x] Escrow via authorize / capture / partial capture / void
- [x] Double-entry ledger with locks and idempotency
- [x] 8-check policy engine and trust tiers
- [x] Preview-before-payment (`402` until approved)
- [ ] Paid tool gateway that answers `402` with a price quote
- [ ] LLM agent runtime with tool calling
- [ ] PayPal webhooks with event dedupe (table already exists)
- [ ] Dashboard (Next.js + AG Grid) and phone approval page
- [ ] Vendor payouts via PayPal Payouts

## Note

Sandbox-only hackathon project. Holding funds on behalf of others in production would need regulatory review.

## License

MIT
