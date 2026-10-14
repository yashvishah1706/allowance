"""Settings loaded from environment variables (see .env.example)."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # "sandbox" talks to the real PayPal sandbox; "fake" runs fully offline for local demos and tests.
    paypal_mode: str = os.getenv("PAYPAL_MODE", "sandbox")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./allowance.db")
    paypal_client_id: str = os.getenv("PAYPAL_CLIENT_ID", "")
    paypal_client_secret: str = os.getenv("PAYPAL_CLIENT_SECRET", "")
    paypal_base_url: str = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
    return_url: str = os.getenv("PAYPAL_RETURN_URL", "http://localhost:8000/paypal/return")
    cancel_url: str = os.getenv("PAYPAL_CANCEL_URL", "http://localhost:8000/paypal/cancel")


settings = Settings()

# 1 credit = $0.10. Credits are integers so the ledger never deals with float money.
CENTS_PER_CREDIT = 10


def credits_to_usd(credits: int) -> str:
    """30 credits -> '3.00' (the string format PayPal expects)."""
    cents = credits * CENTS_PER_CREDIT
    return f"{cents // 100}.{cents % 100:02d}"
