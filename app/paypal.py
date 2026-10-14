"""Thin PayPal Orders/Payments v2 client (sandbox by default).

Escrow mapping:
  post task   -> create_order(intent=AUTHORIZE)  buyer approves in browser
  fund task   -> authorize_order                 reward is HELD
  approve     -> capture(full amount)            reward is paid
  reject      -> capture(expenses only, final)   PayPal releases the rest
                 or void() when there were no expenses
"""

import time
import uuid

import requests

from .config import settings


class PayPalError(Exception):
    def __init__(self, step: str, status: int, body: str, debug_id: str | None):
        super().__init__(f"PayPal {step} failed: HTTP {status} (debug id {debug_id}) {body[:300]}")
        self.status = status


class PayPalClient:
    def __init__(self, client_id: str | None = None, secret: str | None = None, base_url: str | None = None):
        self.client_id = client_id or settings.paypal_client_id
        self.secret = secret or settings.paypal_client_secret
        self.base_url = (base_url or settings.paypal_base_url).rstrip("/")
        self._token: str | None = None
        self._token_expiry = 0.0

    # ---- auth ----
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        if not self.client_id or not self.secret:
            raise RuntimeError("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET")
        resp = requests.post(f"{self.base_url}/v1/oauth2/token", auth=(self.client_id, self.secret),
                             data={"grant_type": "client_credentials"}, timeout=30)
        self._check(resp, "get access token")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3000))
        return self._token

    def _headers(self, request_id: str | None = None) -> dict:
        # PayPal-Request-Id makes POSTs idempotent: a retry with the same id is not repeated.
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": request_id or str(uuid.uuid4()),
        }

    @staticmethod
    def _check(resp, step: str):
        if resp.status_code >= 400:
            raise PayPalError(step, resp.status_code, resp.text, resp.headers.get("PayPal-Debug-Id"))

    # ---- escrow operations ----
    def create_authorize_order(self, amount_usd: str, description: str, reference_id: str, request_id: str) -> dict:
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "reference_id": reference_id,
                "description": description[:127],
                "amount": {"currency_code": "USD", "value": amount_usd},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": "Allowance", "user_action": "CONTINUE",
                "return_url": settings.return_url, "cancel_url": settings.cancel_url,
            }}},
        }
        resp = requests.post(f"{self.base_url}/v2/checkout/orders", json=body,
                             headers=self._headers(request_id), timeout=30)
        self._check(resp, "create order")
        order = resp.json()
        approve = next((l["href"] for l in order.get("links", []) if l["rel"] in ("payer-action", "approve")), None)
        return {"order_id": order["id"], "approve_url": approve}

    def create_capture_order(self, amount_usd: str, description: str, reference_id: str, request_id: str) -> dict:
        """Used for top-ups: money is taken immediately once the buyer approves and we capture."""
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{"reference_id": reference_id, "description": description[:127],
                                "amount": {"currency_code": "USD", "value": amount_usd}}],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": "Allowance", "user_action": "PAY_NOW",
                "return_url": settings.return_url, "cancel_url": settings.cancel_url,
            }}},
        }
        resp = requests.post(f"{self.base_url}/v2/checkout/orders", json=body,
                             headers=self._headers(request_id), timeout=30)
        self._check(resp, "create top-up order")
        order = resp.json()
        approve = next((l["href"] for l in order.get("links", []) if l["rel"] in ("payer-action", "approve")), None)
        return {"order_id": order["id"], "approve_url": approve}

    def capture_order(self, order_id: str, request_id: str) -> dict:
        resp = requests.post(f"{self.base_url}/v2/checkout/orders/{order_id}/capture", json={},
                             headers=self._headers(request_id), timeout=30)
        self._check(resp, "capture top-up order")
        cap = resp.json()["purchase_units"][0]["payments"]["captures"][0]
        return {"capture_id": cap["id"], "status": cap["status"]}

    def authorize_order(self, order_id: str, request_id: str) -> dict:
        resp = requests.post(f"{self.base_url}/v2/checkout/orders/{order_id}/authorize", json={},
                             headers=self._headers(request_id), timeout=30)
        self._check(resp, "authorize order")
        auth = resp.json()["purchase_units"][0]["payments"]["authorizations"][0]
        return {"authorization_id": auth["id"], "status": auth["status"], "expires": auth.get("expiration_time")}

    def capture(self, authorization_id: str, amount_usd: str | None, request_id: str) -> dict:
        body: dict = {"final_capture": True}
        if amount_usd is not None:
            body["amount"] = {"currency_code": "USD", "value": amount_usd}
        resp = requests.post(f"{self.base_url}/v2/payments/authorizations/{authorization_id}/capture",
                             json=body, headers=self._headers(request_id), timeout=30)
        self._check(resp, "capture authorization")
        cap = resp.json()
        return {"capture_id": cap["id"], "status": cap["status"]}

    def void(self, authorization_id: str, request_id: str) -> dict:
        resp = requests.post(f"{self.base_url}/v2/payments/authorizations/{authorization_id}/void",
                             headers=self._headers(request_id), timeout=30)
        self._check(resp, "void authorization")
        return {"status": "VOIDED"}


class FakePayPal:
    """In-memory stand-in used by tests and by the offline demo (PAYPAL_MODE=fake)."""

    def __init__(self):
        self.calls: list[tuple] = []
        self._n = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}-{self._n}"

    def create_authorize_order(self, amount_usd, description, reference_id, request_id):
        self.calls.append(("create", amount_usd, reference_id))
        oid = self._id("ORDER")
        return {"order_id": oid, "approve_url": f"https://sandbox.paypal.test/approve/{oid}"}

    def create_capture_order(self, amount_usd, description, reference_id, request_id):
        self.calls.append(("create_capture", amount_usd, reference_id))
        oid = self._id("ORDER")
        return {"order_id": oid, "approve_url": f"https://sandbox.paypal.test/approve/{oid}"}

    def capture_order(self, order_id, request_id):
        self.calls.append(("capture_order", order_id))
        return {"capture_id": self._id("CAP"), "status": "COMPLETED"}

    def authorize_order(self, order_id, request_id):
        self.calls.append(("authorize", order_id))
        return {"authorization_id": self._id("AUTH"), "status": "CREATED", "expires": None}

    def capture(self, authorization_id, amount_usd, request_id):
        self.calls.append(("capture", authorization_id, amount_usd))
        return {"capture_id": self._id("CAP"), "status": "COMPLETED"}

    def void(self, authorization_id, request_id):
        self.calls.append(("void", authorization_id))
        return {"status": "VOIDED"}
