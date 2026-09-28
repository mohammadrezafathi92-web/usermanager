"""Read-only report: which PackageConnection rows would be REJECTED if this
installation turned on package-authorization enforcement
(PanelSettings.package_node_scope_enforced) right now.

Phase C (docs/api-key-scope-audit-2026-09-27.md, نسخه‌ی هشتم بخش ۷) closes a
real gap: routers/packages.py's package editor never checked that a node
bundled into a package is actually granted to that package's own owner
(AdminNodeAccess/Node.owner_admin_id) - so a package could reference a node
its owner has no real access to, and turning on enforcement would then
start rejecting a purchase of that exact package the moment it happens.
This script finds those rows AHEAD of time, on THIS installation's own
database, so an operator can fix them before flipping the switch - the
switch itself (PUT /api/settings/package-node-scope) re-runs this SAME
check inside a locked transaction and refuses with 409 if anything is still
mismatched, so this script's report going stale can never make the switch
unsafe - it's an early warning, not the actual gate.

DOES NOT MODIFY ANYTHING. Every mismatch found here needs one of exactly
two real fixes - there is no third "just note it and move on" option:
  1. Grant the missing access (a new AdminNodeAccess row for that owner, or
     an ownership fix on the Node itself), or
  2. Fix the PackageConnection/the package's own owner_admin_id so it no
     longer references that node.
Neither of those is done by this script - it only tells you where to look.

Usage - run it INSIDE the backend container, not on the host (see
report_orphan_connections.py in this same directory for the full docker
exec command and why):

    docker exec -it -w /app usermanager-backend \\
        python3 "$PWD/backend/scripts/preflight_package_node_scope.py"
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app import models  # noqa: E402
    from app.database import SessionLocal  # noqa: E402
    from app.services.bot_auth import _find_package_node_scope_mismatches  # noqa: E402
except ModuleNotFoundError as exc:
    print(
        f"\n[!] ماژول «{exc.name}» پیدا نشد.\n\n"
        "این اسکریپت باید داخل کانتینر بک‌اند اجرا بشه، نه روی خود سرور -\n"
        "کتابخونه‌های پایتون پنل فقط داخل کانتینرن.\n\n"
        "دستور درست (از پوشه‌ی نصب پنل):\n\n"
        "    docker exec -it -w /app usermanager-backend \\\n"
        '        python3 "$PWD/backend/scripts/preflight_package_node_scope.py"\n',
        file=sys.stderr,
    )
    sys.exit(2)


def main() -> int:
    db = SessionLocal()
    try:
        settings = db.get(models.PanelSettings, 1)
        already_on = bool(settings and settings.package_node_scope_enforced)
        if already_on:
            print("توجه: package_node_scope_enforced از قبل روی این نصب فعال است - این گزارش صرفاً تاییدی است.\n")

        mismatches = _find_package_node_scope_mismatches(db)
        if not mismatches:
            print("هیچ ناهماهنگی‌ای پیدا نشد - این نصب آماده‌ی فعال‌سازی package-authorization است.")
            print("PUT /api/settings/package-node-scope (با رمز تایید) را می‌زنید تا فعال شود.")
            return 0

        print(f"{len(mismatches)} ناهماهنگی پیدا شد - هرکدام باید قبل از فعال‌سازی رفع شود:\n")
        for m in mismatches:
            package = db.get(models.Package, m["package_id"])
            owner = db.get(models.AdminUser, m["owner_admin_id"])
            node = db.get(models.Node, m["node_id"])
            print(
                f"  - پکیج #{m['package_id']} ({package.name if package else '؟'}), "
                f"مالک=admin#{m['owner_admin_id']} ({owner.username if owner else '؟'}), "
                f"نود#{m['node_id']} ({node.name if node else '؟'}) - این مالک به این نود دسترسی ندارد"
            )
        print(
            "\nهر مورد را با یکی از این دو راه رفع کنید، نه چیز دیگری:\n"
            "  ۱) دسترسی درست را grant کنید (AdminNodeAccess جدید یا اصلاح مالکیت خودِ نود)، یا\n"
            "  ۲) اتصال بسته‌شده/مالک پکیج را در «مدیریت پکیج‌ها» اصلاح کنید.\n"
            "بعد از رفع همه، این اسکریپت را دوباره اجرا کنید تا گزارش خالی شود."
        )
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
