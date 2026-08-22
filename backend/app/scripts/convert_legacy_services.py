"""Converts customers still on the old shared pool into real, independent
services - the backlog the one-time startup migration cannot reach.

That migration is guarded by PanelSettings.legacy_purchases_migrated, so it
ran exactly once. Every customer created afterwards through the bot's
brand-new-customer path landed on the shared pool anyway (fixed in
routers/bot.py's create_user), and those have been accumulating ever since
with nothing to pick them up.

This is that pickup, and it is deliberately a script rather than another
startup hook: it touches quota and expiry on live paying customers, so it
should run when someone decides to run it, having read what it will do.

REPORTS BY DEFAULT, CHANGES NOTHING. Pass --apply to write.

    docker compose exec backend python -m app.scripts.convert_legacy_services
    docker compose exec backend python -m app.scripts.convert_legacy_services --apply

The conversion itself is services/purchase_migration.migrate_user - the
exact function the original migration used, not a second implementation of
the same rules. For a customer with one service that means the Purchase
copies their quota/usage/expiry 1:1, which is no behavioural change at all;
it is the same numbers, finally attached to the service that owns them.
"""
from __future__ import annotations

import sys

from sqlalchemy.orm import selectinload

from ..database import SessionLocal
from .. import models
from ..services import purchase_migration


def _fmt_gb(byte_count: int | None) -> str:
    if not byte_count:
        return "نامحدود"
    return f"{byte_count / (1024 ** 3):.1f}GB"


def main() -> int:
    apply = "--apply" in sys.argv
    db = SessionLocal()
    try:
        users = (
            db.query(models.User)
            .options(selectinload(models.User.connections))
            .join(models.Connection)
            .filter(models.Connection.purchase_id.is_(None))
            .distinct()
            .all()
        )

        if not users:
            print("هیچ مشتری‌ای روی سرویس اشتراکی قدیمی نمانده است.")
            return 0

        print(f"{len(users)} مشتری روی سرویس اشتراکی قدیمی هستند:\n")
        print(f"  {'مشتری':<26}{'اتصال':>6}{'سهمیه':>12}{'مصرف':>12}  وضعیت")

        converted = skipped = failed = 0
        for user in users:
            legacy = [c for c in user.connections if c.purchase_id is None]
            try:
                made, left = purchase_migration.migrate_user(db, user)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
                failed += 1
                print(f"  {user.username:<26}{len(legacy):>6}{'':>24}  خطا: {exc}")
                db.rollback()
                continue

            converted += made
            skipped += left
            if made and not left:
                note = f"{made} سرویس مستقل می‌شود"
            elif made and left:
                note = f"{made} سرویس مستقل، {left} گروه ناشناس اشتراکی می‌ماند"
            elif left:
                note = f"{left} گروه پکیجش شناسایی نشد - دست‌نخورده می‌ماند"
            else:
                note = "چیزی برای تبدیل نداشت"

            print(
                f"  {user.username:<26}{len(legacy):>6}"
                f"{_fmt_gb(user.total_quota_bytes):>12}{_fmt_gb(user.used_bytes):>12}  {note}"
            )

        print()
        if apply:
            db.commit()
            print(f"انجام شد: {converted} سرویس مستقل ساخته شد"
                  + (f"، {skipped} گروه اشتراکی ماند" if skipped else "")
                  + (f"، {failed} مشتری با خطا رد شد" if failed else ""))
            print("سهمیه و انقضا عیناً منتقل شدند - چیزی برای مشتری کم یا زیاد نشده است.")
        else:
            # Nothing above was committed; the objects were built in memory
            # only so the report could describe the real outcome rather than
            # a guess at it.
            db.rollback()
            print(f"این فقط گزارش بود - هیچ تغییری ذخیره نشد. {converted} سرویس مستقل ساخته می‌شود"
                  + (f" و {skipped} گروه اشتراکی می‌ماند" if skipped else "") + ".")
            print("برای اعمال: همین دستور را با --apply اجرا کنید.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
