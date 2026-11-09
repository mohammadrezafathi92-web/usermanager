"""The bot/mini-app service list carries the first-connection days.

Run:  python3 backend/tests/test_bot_purchase_info_first_use.py

Bug (production, 2026-11-09): /api/miniapp/home answered 500 for every
customer ('BotPurchaseInfo' object has no attribute
'expire_days_after_first_use') since the first-connection feature: the
mini app reads that field from list_user_purchases' items.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "bot-purchase-info-first-use-test-0123456789")

from app import models, schemas

purchase = models.Purchase(id=7, quota_bytes=1, used_bytes=0, expire_at=None, expire_days_after_first_use=30,
                           status=models.UserStatus.active, created_at=dt.datetime.utcnow())
try:
    info = schemas.BotPurchaseInfo.model_validate(purchase)
    got = info.expire_days_after_first_use
except Exception as exc:  # noqa: BLE001
    got = f"raised {type(exc).__name__}: {exc}"
if got != 30:
    print(f"FAIL  BotPurchaseInfo carries expire_days_after_first_use\n        got: {got!r}")
    sys.exit(1)
print("PASS  BotPurchaseInfo carries expire_days_after_first_use")
