"""
Allowance - Day 1 escrow test (PayPal sandbox)

Proves the core money flow for task rewards:
  1. Create an order with intent AUTHORIZE   (human posts a task with a reward)
  2. Sandbox buyer approves it in the browser
  3. Authorize the order                      (reward is HELD, not taken)
  4a. approve mode -> capture the authorization (work accepted, agent gets paid)
  4b. reject mode  -> void the authorization    (work rejected, hold released)

Usage:
  pip install requests
  export PAYPAL_CLIENT_ID=...      # from your sandbox app
  export PAYPAL_CLIENT_SECRET=...
  python escrow_demo.py approve    # authorize -> capture
  python escrow_demo.py reject     # authorize -> void
"""

import os
import sys
import uuid

import requests

BASE_URL = "https://api-m.sandbox.paypal.com"
REWARD_USD = "3.00"  # 30 credits at 1 credit = $0.10


def get_access_token() -> str:
    client_id = os.environ.get("PAYPAL_CLIENT_ID")
    secret = os.environ.get("PAYPAL_CLIENT_SECRET")
    if not client_id or not secret:
        sys.exit("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET first.")

    resp = requests.post(
        f"{BASE_URL}/v1/oauth2/token",
        auth=(client_id, secret),
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    check(resp, "get access token")
    return resp.json()["access_token"]


def headers(token: str) -> dict:
    # A fresh PayPal-Request-Id per call makes each POST idempotent:
    # if a retry sends the same id, PayPal won't repeat the action.
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "PayPal-Request-Id": str(uuid.uuid4()),
    }


def check(resp: requests.Response, step: str) -> None:
    if resp.status_code >= 400:
        debug_id = resp.headers.get("PayPal-Debug-Id", "n/a")
        sys.exit(
            f"\n[FAILED] {step}: HTTP {resp.status_code} (debug id {debug_id})\n{resp.text}"
        )


def create_order(token: str) -> tuple[str, str]:
    body = {
        "intent": "AUTHORIZE",
        "purchase_units": [
            {
                "reference_id": "task-demo-001",
                "description": "Allowance task reward: Research 5 fintech startups",
                "amount": {"currency_code": "USD", "value": REWARD_USD},
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "brand_name": "Allowance",
                    "user_action": "CONTINUE",
                    "return_url": "https://example.com/return",
                    "cancel_url": "https://example.com/cancel",
                }
            }
        },
    }
    resp = requests.post(
        f"{BASE_URL}/v2/checkout/orders", json=body, headers=headers(token), timeout=30
    )
    check(resp, "create order")
    order = resp.json()

    approve_link = next(
        (l["href"] for l in order.get("links", []) if l["rel"] in ("payer-action", "approve")),
        None,
    )
    if not approve_link:
        sys.exit(f"No approval link returned:\n{order}")
    return order["id"], approve_link


def authorize_order(token: str, order_id: str) -> str:
    resp = requests.post(
        f"{BASE_URL}/v2/checkout/orders/{order_id}/authorize",
        json={},
        headers=headers(token),
        timeout=30,
    )
    check(resp, "authorize order")
    data = resp.json()
    auth = data["purchase_units"][0]["payments"]["authorizations"][0]
    print(f"  Authorization {auth['id']} status: {auth['status']}")
    if auth.get("expiration_time"):
        print(f"  Hold expires: {auth['expiration_time']}")
    return auth["id"]


def capture_authorization(token: str, auth_id: str) -> None:
    resp = requests.post(
        f"{BASE_URL}/v2/payments/authorizations/{auth_id}/capture",
        json={"final_capture": True},
        headers=headers(token),
        timeout=30,
    )
    check(resp, "capture authorization")
    cap = resp.json()
    print(f"  Capture {cap['id']} status: {cap['status']}")


def void_authorization(token: str, auth_id: str) -> None:
    resp = requests.post(
        f"{BASE_URL}/v2/payments/authorizations/{auth_id}/void",
        headers=headers(token),
        timeout=30,
    )
    check(resp, "void authorization")
    # 204 No Content on success (or 200 with a body, depending on Prefer header)
    print(f"  Void succeeded (HTTP {resp.status_code})")


def get_authorization_status(token: str, auth_id: str) -> str:
    resp = requests.get(
        f"{BASE_URL}/v2/payments/authorizations/{auth_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    check(resp, "get authorization")
    return resp.json()["status"]


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "approve"
    if mode not in ("approve", "reject"):
        sys.exit("Usage: python escrow_demo.py [approve|reject]")

    print("1. Getting access token...")
    token = get_access_token()

    print(f"2. Creating AUTHORIZE order for ${REWARD_USD} (task reward)...")
    order_id, approve_link = create_order(token)
    print(f"  Order {order_id} created.")

    print("\n3. Open this link and log in with your SANDBOX PERSONAL (buyer) account:")
    print(f"\n   {approve_link}\n")
    print("   After approving, you'll land on example.com. That's expected.")
    input("   Press Enter here once you've approved... ")

    print("\n4. Authorizing (reward is now HELD, not charged)...")
    auth_id = authorize_order(token, order_id)

    if mode == "approve":
        print("\n5. Human APPROVED the work -> capturing reward...")
        capture_authorization(token, auth_id)
    else:
        print("\n5. Human REJECTED the work -> voiding the hold...")
        void_authorization(token, auth_id)

    final = get_authorization_status(token, auth_id)
    print(f"\nFinal authorization status: {final}")
    expected = "CAPTURED" if mode == "approve" else "VOIDED"
    print("PASS" if final == expected else f"CHECK: expected {expected}")


if __name__ == "__main__":
    main()
