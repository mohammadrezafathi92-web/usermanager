#!/usr/bin/env python3
"""Find every stored Telegram id that is actually one of this panel's BOTS.

    docker compose exec backend python scripts/report_bot_id_targets.py

Read-only - it prints, it never writes.

Why: Telegram refuses to let one bot message another, with

    Forbidden: the bot can't send messages to the bot

and the panel's notification sends are all deliberately best-effort, so
that refusal is swallowed and the receipt/approval/backup simply never
arrives. Nothing in the panel invents such an id; it gets typed in, because
a bot token's prefix (the digits before the ":") IS that bot's Telegram user
id, and it is the number sitting on screen right after @BotFather hands over
a token.

The panel now refuses one at the point of entry (services/telegram_ids.py),
but that does nothing for the ones already saved - this finds those.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:////app/data/app.db")

from app import models  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.services.telegram_ids import panel_bot_ids  # noqa: E402
from app.telegram_bot.config import parse_id_set  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        bots = panel_bot_ids(db)
        print(f"ربات‌های این پنل: {len(bots)}")
        for bot_id, label in sorted(bots.items()):
            print(f"  {bot_id}  ({label})")
        if not bots:
            print("هیچ توکن رباتی ثبت نشده - چیزی برای بررسی نیست.")
            return 0

        problems: list[str] = []

        for admin in db.query(models.AdminUser).filter(models.AdminUser.telegram_id.isnot(None)).all():
            if admin.telegram_id in bots:
                problems.append(
                    f"ادمین «{admin.username}» (#{admin.id}): آیدی تلگرام {admin.telegram_id} "
                    f"= {bots[admin.telegram_id]}\n"
                    f"    → صفحه‌ی «ادمین‌ها» ← ویرایش این ادمین ← آیدی عددی تلگرام را با آیدی خودِ شخص عوض کنید"
                )

        shared = db.get(models.BotSettings, 1)
        if shared is not None:
            for field, where in (("admin_ids", "آیدی عددی ادمین‌ها"), ("approval_chat_ids", "چت‌های تایید رسید")):
                for tg_id in parse_id_set(getattr(shared, field, "") or ""):
                    if tg_id in bots:
                        problems.append(
                            f"تنظیمات ربات مشترک، «{where}»: {tg_id} = {bots[tg_id]}\n"
                            f"    → تنظیمات ← ربات تلگرام ← این عدد را از فهرست بردارید"
                        )

        for card in db.query(models.PaymentCard).filter(models.PaymentCard.approval_telegram_id.isnot(None)).all():
            if card.approval_telegram_id in bots:
                owner = "پنل اصلی" if card.owner_admin_id is None else f"ادمین #{card.owner_admin_id}"
                problems.append(
                    f"کارت پرداخت #{card.id} ({owner}، {card.card_number}): "
                    f"آیدی تایید {card.approval_telegram_id} = {bots[card.approval_telegram_id]}\n"
                    f"    → تنظیمات ← کارت‌ها ← ویرایش این کارت ← آیدی تایید"
                )

        print()
        if not problems:
            print("✅ هیچ آیدی رباتی به‌عنوان مقصد اعلان ذخیره نشده است.")
            return 0
        print(f"⚠️  {len(problems)} مورد پیدا شد - اعلان‌های این موارد به هیچ‌کس نمی‌رسد:\n")
        for p in problems:
            print("  - " + p)
        print(
            "\nآیدی عددی درست هر شخص را می‌تواند با فرستادن /start به @userinfobot بگیرد."
        )
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
