"""A reseller must never be able to write the MAIN panel's payment/support row.

Run:  python3 backend/tests/test_shared_settings_isolation.py

Reported 2026-09-12: "دیتای ایدی پشتیبان و اطلاعات پرداختش هی میاد میشینه
روی پنل اصلی" - a level-2 Admin's own support id and card details kept
landing on the main panel. /api/settings is open to require_admin_or_above
(a level-2 Admin legitimately READS this row: it is the fallback behind
every field they have not set on their own account), and the settings page
posted the whole loaded object back on every save - so the shared
PanelSettings row picked up whatever they typed, and it stayed there as
every other reseller's fallback too.

What this file pins down:

  * a non-superadmin cannot CHANGE any of the shared customer-facing
    fields (403), and
  * they can still save the row when those fields come back unchanged -
    which they always do, since the page loads and resubmits them. Getting
    this half wrong would lock a level-2 Admin out of the settings page
    entirely, which is exactly the mistake the balance field made in
    routers/admins.py.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.routers.panel_settings import SUPERADMIN_ONLY_SETTINGS_FIELDS, update_settings

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(models.PanelSettings(
        id=1,
        payment_card_number="6037-0000-0000-0001",
        payment_card_holder="مدیر پنل",
        support_contact_text="@panel_support",
        panel_public_url="https://panel.example.com",
    ))
    db.commit()
    return db


def put(db, admin, **fields):
    """Returns "ok" or the HTTP status the endpoint refused with."""
    try:
        update_settings(schemas.PanelSettingsUpdate(**fields), db=db, admin=admin)
        return "ok"
    except HTTPException as exc:
        return exc.status_code


superadmin = models.AdminUser(id=1, username="root", hashed_password="x", is_superadmin=True)
reseller = models.AdminUser(id=2, username="reseller", hashed_password="x", is_superadmin=False, role="admin")

print("--- a level-2 Admin cannot change the shared row ---")
db = make_db()
check("support id refused", put(db, reseller, support_contact_text="@their_own_support"), 403)
check("...and the main panel kept its own",
      db.get(models.PanelSettings, 1).support_contact_text, "@panel_support")
check("card number refused", put(db, reseller, payment_card_number="5022-1111-2222-3333"), 403)
check("...and the main panel kept its own",
      db.get(models.PanelSettings, 1).payment_card_number, "6037-0000-0000-0001")
check("card holder refused", put(db, reseller, payment_card_holder="اسم فروشنده"), 403)
check("public url refused", put(db, reseller, panel_public_url="https://their-own.example.com"), 403)
check("clearing a field counts as a change too", put(db, reseller, support_contact_text=""), 403)

print("\n--- but resubmitting them unchanged (what the page does on every save) is fine ---")
row = db.get(models.PanelSettings, 1)
check("whole row posted back untouched",
      put(db, reseller,
          support_contact_text=row.support_contact_text,
          payment_card_number=row.payment_card_number,
          payment_card_holder=row.payment_card_holder,
          panel_public_url=row.panel_public_url,
          # a field the reseller IS allowed to move, alongside them
          loyalty_reward_credit=5000),
      "ok")
check("...and their own edit went through", db.get(models.PanelSettings, 1).loyalty_reward_credit, 5000)
check("a trailing slash on the url is not mistaken for an edit",
      put(db, reseller, panel_public_url=row.panel_public_url + "/"), "ok")

print("\n--- the superadmin still owns all of it ---")
check("superadmin sets the support id", put(db, superadmin, support_contact_text="@new_panel_support"), "ok")
check("...and it stuck", db.get(models.PanelSettings, 1).support_contact_text, "@new_panel_support")
check("superadmin sets the card", put(db, superadmin, payment_card_number="6219-9999-8888-7777"), "ok")

print("\n--- HA stays superadmin-only, as before ---")
check("reseller refused HA", put(db, reseller, ha_peer_api_key="stolen"), 403)

print("\n--- every guarded field is a real PanelSettings column ---")
unknown = sorted(f for f in SUPERADMIN_ONLY_SETTINGS_FIELDS if not hasattr(models.PanelSettings, f))
check("no guarded field is a typo", unknown, [])
not_in_schema = sorted(
    f for f in SUPERADMIN_ONLY_SETTINGS_FIELDS if f not in schemas.PanelSettingsUpdate.model_fields
)
check("every guarded field is actually writable through this endpoint", not_in_schema, [])

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("اطلاعات پرداخت و پشتیبانی پنل اصلی از دسترس نمایندگان خارج است")
