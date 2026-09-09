"""routers/bot.py's list_packages() - a package with seller_visible=False
must be hidden from a Seller in the Telegram bot too, not just the web
panel's Packages page (routers/packages.py's list_packages already did
this).

Run:  python3 backend/tests/test_bot_seller_visible.py

Bug reported 2026-09-09: "اون اپدیت نشون دادن پکیج به فروشنده ها بود فقط
توی پنل کار میکنه توی بات فروشنده ها نمایش داده میشه" (that package-
visibility-to-sellers toggle only works in the panel, the bot still shows
it to sellers). models.Package.seller_visible's own docstring used to
claim this was panel-only "by design" - but routers/bot.py's list_packages
is the exact function both a Seller's admin-menu package picker (creating/
renewing a purchase for their own customer) AND that Seller's own
dedicated bot's customer-facing checkout (AdminUser.own_bot_token) call
through, with owner_admin_id set to the Seller's own id either way - so a
hidden package was still fully offered/purchasable there."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models  # noqa: E402
from app.routers import bot as bot_router  # noqa: E402
from app.services import hierarchy  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

admin = models.AdminUser(username="admin1", hashed_password="x", is_superadmin=False, role="admin")
db.add(admin)
db.commit()
db.refresh(admin)
hierarchy.rebuild_path(db, admin)
db.commit()

seller = models.AdminUser(username="seller1", hashed_password="x", is_superadmin=False, role="seller", parent_admin_id=admin.id)
db.add(seller)
db.commit()
db.refresh(seller)
hierarchy.rebuild_path(db, seller)
db.commit()

pkg_visible = models.Package(name="پکیج عمومی", quota_gb=10, duration_days=30, price=1000, owner_admin_id=admin.id, bot_enabled=True, seller_visible=True)
pkg_hidden = models.Package(name="پکیج داخلی", quota_gb=10, duration_days=30, price=1000, owner_admin_id=admin.id, bot_enabled=True, seller_visible=False)
db.add_all([pkg_visible, pkg_hidden])
db.commit()

print("--- the owning Admin's own bot session sees BOTH packages (seller_visible never restricts an Admin) ---")
pkgs = bot_router.list_packages(owner_admin_id=admin.id, db=db)
names = {p.name for p in pkgs}
check("both visible to the Admin", names, {"پکیج عمومی", "پکیج داخلی"})

print("\n--- the Seller's bot session (admin-menu picker / their own dedicated bot) only sees the seller_visible one ---")
pkgs = bot_router.list_packages(owner_admin_id=seller.id, db=db)
names = {p.name for p in pkgs}
check("only the seller-visible package", names, {"پکیج عمومی"})

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
