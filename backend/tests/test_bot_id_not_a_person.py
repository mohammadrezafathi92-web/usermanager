"""A bot's own Telegram id must never be saved as a person's id.

Run:  python3 backend/tests/test_bot_id_not_a_person.py

Reported 2026-09-12, from a live panel's logs:

    TelegramForbiddenError: Telegram server says -
    Forbidden: the bot can't send messages to the bot

A bot token is "<bot's telegram user id>:<secret>", so the digits before
the colon are a real, valid-looking Telegram id sitting right in front of
whoever just made the bot - and the next box they fill in asks for a
numeric id. The panel accepted it and then every notification aimed at that
"admin" was dropped by Telegram, invisibly, because those sends are
best-effort by design. On an admin's own row it is worse still: the same id
decides who gets the admin menu, so the real person keeps getting the
customer menu with no explanation.

This pins the guard down: it must catch a bot id, and it must NOT get in
the way of an ordinary one.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import telegram_ids

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


SHARED_BOT_ID = 8012345678
OWN_BOT_ID = 7099887766
PERSON_ID = 123456789


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(models.BotSettings(id=1, bot_token=f"{SHARED_BOT_ID}:AAHshared-secret"))
    db.add(models.AdminUser(
        id=2, username="reseller", hashed_password="x",
        own_bot_token=f"{OWN_BOT_ID}:AAHown-secret",
    ))
    db.add(models.AdminUser(id=3, username="notoken", hashed_password="x"))
    db.commit()
    return db


print("--- reading a bot's id out of its token ---")
check("a normal token", telegram_ids.id_from_token("8012345678:AAHxyz"), 8012345678)
check("whitespace around it", telegram_ids.id_from_token("  8012345678:AAHxyz "), 8012345678)
check("no colon at all", telegram_ids.id_from_token("nonsense"), None)
check("empty", telegram_ids.id_from_token(""), None)
check("None", telegram_ids.id_from_token(None), None)

print("\n--- which ids belong to this panel's own bots ---")
db = make_db()
found = telegram_ids.panel_bot_ids(db)
check("both the shared bot and the reseller's own are known", sorted(found), sorted([SHARED_BOT_ID, OWN_BOT_ID]))
check("the shared one is named as such", found[SHARED_BOT_ID], "ربات مشترک پنل")
check("the reseller's own names the reseller", found[OWN_BOT_ID], "ربات اختصاصی «reseller»")

print("\n--- the guard ---")
check("the shared bot's id is refused", bool(telegram_ids.describe_if_bot(db, SHARED_BOT_ID)), True)
check("a reseller's own bot id is refused", bool(telegram_ids.describe_if_bot(db, OWN_BOT_ID)), True)
check("the message says where to get the right one",
      "@userinfobot" in (telegram_ids.describe_if_bot(db, SHARED_BOT_ID) or ""), True)
check("an ordinary person's id passes", telegram_ids.describe_if_bot(db, PERSON_ID), None)
check("None passes (clearing the field)", telegram_ids.describe_if_bot(db, None), None)
check("0 passes (the 'clear it' convention)", telegram_ids.describe_if_bot(db, 0), None)

print("\n--- a panel with no bot token configured refuses nothing ---")
empty = sessionmaker(bind=create_engine("sqlite://", connect_args={"check_same_thread": False}))()
models.Base.metadata.create_all(empty.get_bind())
check("no bots known", telegram_ids.panel_bot_ids(empty), {})
check("...so any id is fine", telegram_ids.describe_if_bot(empty, SHARED_BOT_ID), None)

print("\n--- scanning ids that are already saved ---")
check("picks the bots out of a mixed list",
      telegram_ids.bot_ids_among(db, [PERSON_ID, SHARED_BOT_ID, 555, OWN_BOT_ID]),
      sorted([OWN_BOT_ID, SHARED_BOT_ID]))
check("a clean list reports nothing", telegram_ids.bot_ids_among(db, [PERSON_ID, 555]), [])

print("\n--- the routers actually call it ---")
import inspect  # noqa: E402
from app.routers import admins as admins_router, panel_settings, telegram_bot_settings  # noqa: E402

check("create_admin guards the id", "describe_if_bot" in inspect.getsource(admins_router.create_admin), True)
check("update_admin guards the id", "describe_if_bot" in inspect.getsource(admins_router.update_admin), True)
check("the bot settings page guards both id lists",
      inspect.getsource(telegram_bot_settings.update_settings).count("describe_if_bot"), 1)
for fn in ("create_payment_card", "update_payment_card", "create_my_payment_card", "update_my_payment_card"):
    check(f"{fn} guards the approval id",
          "_reject_bot_approval_id" in inspect.getsource(getattr(panel_settings, fn)), True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("آیدی ربات دیگر به‌عنوان آیدی یک شخص ذخیره نمی‌شود")
