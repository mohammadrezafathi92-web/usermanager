"""The main panel's payment details are not readable by a reseller.

Run:  python3 backend/tests/test_shared_payment_not_readable.py

Reported 2026-09-14: «توی ادمین لول دو که میری مشخصات پرداخت های ادمین اصلی
رو نشون میده مثل کارت های ذخیره شده».

The WRITE side of this was fixed long ago - SUPERADMIN_ONLY_SETTINGS_FIELDS
refuses a level-2 Admin changing the shared row. The READ side never was:
GET /api/settings is require_admin_or_above, and it returned the whole
PanelSettings row plus the global saved-card pool to anyone who asked. It
went unnoticed because the settings PAGE hides that section behind
isSuperadmin - the screen was clean and the response was not, which is the
worst combination: nobody looks, and the data is one request away.

What must stay readable matters as much as what must not. panel_public_url
and display_utc_offset_minutes are superadmin-only to WRITE but operational
to READ - blanking the offset would shift every timestamp a reseller sees.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.routers import panel_settings

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()

root = models.AdminUser(id=1, username="root", hashed_password="x", is_superadmin=True)
reseller = models.AdminUser(id=2, username="ali", hashed_password="x")
db.add_all([root, reseller])
db.add(models.PanelSettings(
    id=1,
    payment_card_number="6037-9975-1111-2222",
    payment_card_holder="محمدرضا فتحی",
    payment_instructions="فقط کارت به کارت",
    topup_presets="50000,100000",
    support_contact_text="@main_support",
    panel_public_url="https://panel.example.ir",
    display_utc_offset_minutes=210,
))
db.commit()
# Two cards in the GLOBAL pool - the superadmin's own saved cards.
db.add(models.PaymentCard(owner_admin_id=None, card_number="6037-1", card_holder="الف"))
db.add(models.PaymentCard(owner_admin_id=None, card_number="6037-2", card_holder="ب"))
# ...and one of the reseller's own, which must be unaffected either way.
db.add(models.PaymentCard(owner_admin_id=2, card_number="5892-9", card_holder="علی"))
db.commit()


print("--- the superadmin still sees everything ---")
mine = panel_settings.get_settings(db=db, admin=root)
check("their own card number", mine.payment_card_number, "6037-9975-1111-2222")
check("their own cardholder", mine.payment_card_holder, "محمدرضا فتحی")
check("their own support id", mine.support_contact_text, "@main_support")
check("and their saved cards", sorted(c.card_number for c in mine.payment_cards),
      ["6037-1", "6037-2"])


print("\n--- a level-2 Admin sees none of it ---")
theirs = panel_settings.get_settings(db=db, admin=reseller)
for field in ("payment_card_number", "payment_card_holder", "payment_instructions",
              "topup_presets", "support_contact_text"):
    check(f"{field} is gone", getattr(theirs, field), "")
check("no saved cards at all", theirs.payment_cards, [])
check("...not even the count leaks", len(theirs.payment_cards), 0)

# The point of the whole exercise: the digits must not be anywhere in the
# response, under any field name. A blanked field plus a copy hiding in
# another key is not redaction.
blob = theirs.model_dump_json()
for secret in ("6037-9975-1111-2222", "محمدرضا فتحی", "6037-1", "6037-2", "@main_support"):
    check(f"nothing of {secret!r} survives anywhere in the response",
          secret in blob, False)


print("\n--- but what a reseller's panel needs still works ---")
check("the public url is still readable", theirs.panel_public_url, "https://panel.example.ir")
# Blanking this would silently shift every date and time a reseller sees.
check("the timezone offset is still readable", theirs.display_utc_offset_minutes, 210)


print("\n--- redaction does not touch the database ---")
row = db.get(models.PanelSettings, 1)
check("the shared row is intact", row.payment_card_number, "6037-9975-1111-2222")
check("...and so is the card pool",
      db.query(models.PaymentCard).filter(models.PaymentCard.owner_admin_id.is_(None)).count(), 2)
check("the reseller's own card is untouched",
      db.query(models.PaymentCard).filter(models.PaymentCard.owner_admin_id == 2).count(), 1)

# Read again, as the superadmin, after the redacted read: if _redact_for
# had mutated the ORM row rather than the response object, this would now
# come back empty.
again = panel_settings.get_settings(db=db, admin=root)
check("a redacted read does not poison the next one",
      again.payment_card_number, "6037-9975-1111-2222")



# --------------------------------------------------------------------------
# Two more things a reseller's own panel must get right about money, kept
# here because they are the same complaint: the panel knowing something
# about a reseller's account and not telling them.
print("\n--- where a reseller's customers are told to pay ---")
from app.routers import bot as bot_router  # noqa: E402

# A SECOND reseller, with no saved cards of their own at all. The first one
# already has a card in their pool, and a pool card deliberately overrides
# the single legacy field (see get_payment_info) - so testing the fallback
# on them would be measuring the pool, not the fallback.
bare = models.AdminUser(id=3, username="sara", hashed_password="x",
                        own_bot_token="8000000003:AAsara")
db.add(bare)
db.commit()

# This reseller has set NOTHING of their own. Before this fix the main
# admin's card was handed to their customers - who then paid the wrong
# person, silently and correctly-looking, for as long as nobody noticed.
info = bot_router.get_payment_info(owner_admin_id=3, db=db)
check("no card is offered rather than the main admin's", info.payment_card_number, "")
check("...nor the cardholder", info.payment_card_holder, "")
check("...nor instructions describing someone else's process",
      info.payment_instructions, "")
# These two still fall back on purpose - neither can misdirect a payment.
check("but the support contact still reaches somebody",
      info.support_contact_text, "@main_support")
check("...and the suggested amounts survive", info.topup_presets, "50000,100000")

bare.own_payment_card_number = "5892-1010-2020-3030"
bare.own_payment_card_holder = "علی"
db.commit()
info = bot_router.get_payment_info(owner_admin_id=3, db=db)
check("once they set their own, that is what is shown",
      info.payment_card_number, "5892-1010-2020-3030")
check("...under their own name", info.payment_card_holder, "علی")

# The superadmin's own shop is unaffected by any of this - their customers
# still get whichever card their own pool resolves to, exactly as before.
check("the main admin's own customers still see a main card",
      bot_router.get_payment_info(owner_admin_id=None, db=db).payment_card_number,
      "6037-1")
# And a reseller WITH a pool still gets their own pool's card, not the
# legacy field and certainly not the main admin's.
check("a reseller's own pool card wins for their customers",
      bot_router.get_payment_info(owner_admin_id=2, db=db).payment_card_number, "5892-9")


print("\n--- and what the panel tells them they owe ---")
from app.routers.dashboard import _debt_toman  # noqa: E402


def owing(balance, gb=0.0, rate=0):
    a = models.AdminUser(username="t", hashed_password="x")
    a.balance, a.unbilled_usage_gb, a.wholesale_price_per_gb = balance, gb, rate
    return _debt_toman(a)


check("credit in hand is not a debt", owing(500000), 0)
check("an overdrawn balance is", owing(-120000), 120000)
check("so is traffic metered but not yet priced", owing(0, 12.4, 3000), 37200)
check("and the two together", owing(-120000, 12.4, 3000), 157200)
# The case that makes adding the halves separately wrong: the pending
# charge is going to come out of credit they already hold, so there is no
# debt - a figure they would go looking for and not find.
check("credit that covers the pending charge leaves nothing owed",
      owing(900000, 1, 3000), 0)
check("...and traffic with no price set cannot be owed for",
      owing(0, 12.4, 0), 0)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("کارت‌های ادمین اصلی از چشم فروشنده‌ها دور می‌ماند")
