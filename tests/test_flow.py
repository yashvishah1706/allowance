"""End-to-end API tests with the fake PayPal client: the full demo story."""


def setup_agent(client, starter=5):
    client.post("/seed")
    agent = client.post("/agents", json={"name": "research-agent"}).json()
    t = client.post("/topups", json={"credits": 200}).json()
    assert t["approve_url"]
    assert client.post(f"/topups/{t['id']}/complete").json()["pool_credits"] == 200
    client.post(f"/agents/{agent['id']}/grant", json={"credits": starter, "idempotency_key": "starter-1"})
    return agent["id"]


def tool_id(client, name):
    return next(t["id"] for t in client.get("/tools").json() if t["name"] == name)


def funded_task(client, agent_id, reward=30):
    task = client.post("/tasks", json={"agent_id": agent_id, "title": "Research 5 fintech startups",
                                       "reward_credits": reward}).json()
    assert task["status"] == "awaiting_funding" and task["approve_url"]
    task = client.post(f"/tasks/{task['id']}/fund").json()
    assert task["status"] == "funded"
    return task["id"]


WHY = "Need company basics for the five startups in this task"


def test_happy_path_approve_pays_agent_and_unlocks_report(client, paypal):
    aid = setup_agent(client)
    tid = funded_task(client, aid)

    # new agent must ask; owner approves; purchase completes
    p = client.post(f"/agents/{aid}/purchases", json={"tool_id": tool_id(client, "Company Info"), "task_id": tid,
                                                      "justification": WHY, "idempotency_key": "p1"}).json()
    assert p["status"] == "pending_approval"
    p = client.post(f"/purchases/{p['id']}/review", json={"approve": True}).json()
    assert p["status"] == "completed"
    assert client.get(f"/agents/{aid}").json()["wallet_credits"] == 3

    client.post(f"/tasks/{tid}/submit", json={"preview": "Summary + 1 of 5", "full_report": "All 5 companies"})
    # report is locked before payment
    assert client.get(f"/tasks/{tid}/report").status_code == 402

    done = client.post(f"/tasks/{tid}/approve", json={"rating": 5}).json()
    assert done["status"] == "approved" and done["full_report"] == "All 5 companies"
    assert client.get(f"/tasks/{tid}/report").status_code == 200
    assert client.get(f"/agents/{aid}").json()["wallet_credits"] == 33
    assert ("capture", paypal.calls[-1][1], None) == paypal.calls[-1]  # full capture


def test_reject_covers_expenses_and_releases_the_rest(client, paypal):
    aid = setup_agent(client)
    tid = funded_task(client, aid)
    p = client.post(f"/agents/{aid}/purchases", json={"tool_id": tool_id(client, "Company Info"), "task_id": tid,
                                                      "justification": WHY, "idempotency_key": "p1"}).json()
    client.post(f"/purchases/{p['id']}/review", json={"approve": True})
    client.post(f"/tasks/{tid}/submit", json={"preview": "thin", "full_report": "sloppy"})

    r = client.post(f"/tasks/{tid}/reject", json={"reason": "Missing funding data"}).json()
    assert r["status"] == "rejected" and r["expenses_reimbursed"] == 2
    # PayPal captured only the 2-credit expenses ($0.20); the rest of the hold is released
    assert paypal.calls[-1][0] == "capture" and paypal.calls[-1][2] == "0.20"
    # wallet: 5 - 2 spent + 2 reimbursed
    assert client.get(f"/agents/{aid}").json()["wallet_credits"] == 5
    assert client.get(f"/tasks/{tid}/report").status_code == 402


def test_reject_without_expenses_voids(client, paypal):
    aid = setup_agent(client)
    tid = funded_task(client, aid)
    client.post(f"/tasks/{tid}/submit", json={"preview": "x", "full_report": "y"})
    client.post(f"/tasks/{tid}/reject", json={"reason": "Wrong companies"})
    assert paypal.calls[-1][0] == "void"


def test_blocked_purchase_moves_no_money(client):
    aid = setup_agent(client, starter=50)
    tid = funded_task(client, aid)
    p = client.post(f"/agents/{aid}/purchases", json={"tool_id": tool_id(client, "News Sentiment"), "task_id": tid,
                                                      "justification": WHY, "idempotency_key": "big"}).json()
    assert p["status"] == "blocked" and p["check"] == "max_per_purchase"
    assert client.get(f"/agents/{aid}").json()["wallet_credits"] == 50


def test_purchase_retry_is_idempotent(client):
    aid = setup_agent(client)
    tid = funded_task(client, aid)
    body = {"tool_id": tool_id(client, "Company Info"), "task_id": tid, "justification": WHY,
            "idempotency_key": "retry-me"}
    ids = {client.post(f"/agents/{aid}/purchases", json=body).json()["id"] for _ in range(3)}
    assert len(ids) == 1


def test_three_good_approvals_promote_to_proven(client):
    aid = setup_agent(client)
    for i in range(3):
        tid = funded_task(client, aid, reward=10)
        client.post(f"/tasks/{tid}/submit", json={"preview": "p", "full_report": "r"})
        client.post(f"/tasks/{tid}/approve", json={"rating": 5})
    a = client.get(f"/agents/{aid}").json()
    assert a["trust_level"] == "proven" and a["limits"]["max_per_purchase"] == 5


def test_cannot_approve_before_submission(client):
    aid = setup_agent(client)
    tid = funded_task(client, aid)
    assert client.post(f"/tasks/{tid}/approve", json={"rating": 5}).status_code == 409


def test_double_approve_does_not_pay_twice(client):
    aid = setup_agent(client)
    tid = funded_task(client, aid)
    client.post(f"/tasks/{tid}/submit", json={"preview": "p", "full_report": "r"})
    client.post(f"/tasks/{tid}/approve", json={"rating": 4})
    client.post(f"/tasks/{tid}/approve", json={"rating": 4})
    assert client.get(f"/agents/{aid}").json()["wallet_credits"] == 35
