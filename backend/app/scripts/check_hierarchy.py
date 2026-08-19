"""Read-only consistency check of the 3-tier hierarchy against live data.

Written because "ادمین‌های لول دو و فروشنده‌ها خیلی چیزاشون درست کار نمی‌کنه"
is a symptom, and a symptom can come from either the CODE applying the wrong
rule or the DATA already violating it. Reading the code answers only half of
that. This script answers the other half.

It never writes. Every check states the rule it is enforcing and where that
rule lives, so a failing line points at the code that owns it rather than at
a bare row id.

Run:  docker compose exec backend python -m app.scripts.check_hierarchy
"""
from __future__ import annotations

import sys

from sqlalchemy.orm import Session

from ..database import SessionLocal
from .. import models
from ..services import hierarchy

findings: list[tuple[str, str]] = []       # (severity, message)


def bad(msg: str) -> None:
    findings.append(("خطا", msg))


def warn(msg: str) -> None:
    findings.append(("هشدار", msg))


def _name(admin: models.AdminUser | None) -> str:
    return f"{admin.username}#{admin.id}" if admin else "?"


def print_tree(db: Session, ctx: dict) -> None:
    """The structure itself, before any rule is applied.

    Added because the first real run produced eight identical "این فروشنده
    دسترسی نود دارد ولی بی‌اثر است" lines, which says a rule is being
    broken but not WHY. The why is almost always the shape of the tree:
    role() is derived purely from parent_admin_id, so an account created
    with a parent - even the superadmin's own row - is a level-3 Seller no
    matter what the person creating it meant.
    """
    by_id = ctx["by_id"]
    counts = {}
    for row in db.query(models.User.owner_admin_id).all():
        counts[row.owner_admin_id] = counts.get(row.owner_admin_id, 0) + 1

    grants = {}
    for row in db.query(models.AdminNodeAccess.admin_id).all():
        grants[row.admin_id] = grants.get(row.admin_id, 0) + 1

    from ..permissions import effective_permissions

    label = {
        hierarchy.ROLE_SUPERADMIN: "سوپرادمین",
        hierarchy.ROLE_ADMIN: "ادمین لول ۲",
        hierarchy.ROLE_SELLER: "فروشنده",
    }
    print("ساختار فعلی:")
    print(f"  {'حساب':<22}{'نقش':<14}{'والد':<14}{'مشتری':>7}{'نود':>6}{'مجوز':>6}{'موجودی':>14}")
    for a in sorted(by_id.values(), key=lambda x: x.id):
        parent = by_id.get(a.parent_admin_id) if a.parent_admin_id else None
        print(f"  {a.username + '#' + str(a.id):<22}"
              f"{label[hierarchy.role(a)]:<14}"
              f"{(parent.username if parent else '-'):<14}"
              f"{counts.get(a.id, 0):>7}"
              f"{grants.get(a.id, 0):>6}"
              f"{len(effective_permissions(a)):>6}"
              f"{(a.balance or 0):>14,}")
    print()


def check_roles(db: Session) -> dict:
    admins = db.query(models.AdminUser).all()
    by_id = {a.id: a for a in admins}
    counts = {hierarchy.ROLE_SUPERADMIN: [], hierarchy.ROLE_ADMIN: [], hierarchy.ROLE_SELLER: []}
    for a in admins:
        counts[hierarchy.role(a)].append(a)

    print(f"سوپرادمین: {len(counts[hierarchy.ROLE_SUPERADMIN])}   "
          f"ادمین لول ۲: {len(counts[hierarchy.ROLE_ADMIN])}   "
          f"فروشنده: {len(counts[hierarchy.ROLE_SELLER])}\n")

    for a in admins:
        # Rule: the hierarchy is fixed at exactly 3 levels
        # (hierarchy.can_create_sub_admin / AdminUser.parent_admin_id).
        if a.parent_admin_id is not None:
            parent = by_id.get(a.parent_admin_id)
            if parent is None:
                bad(f"{_name(a)}: والدش (id={a.parent_admin_id}) وجود ندارد - این حساب عملا بی‌صاحب است")
            elif hierarchy.role(parent) == hierarchy.ROLE_SELLER:
                bad(f"{_name(a)}: والدش {_name(parent)} خودش فروشنده است - سطح چهارم، که مجاز نیست")
            if a.is_superadmin:
                bad(f"{_name(a)}: هم سوپرادمین است هم والد دارد - نقشش قابل تشخیص نیست")

    for a in counts[hierarchy.ROLE_SELLER]:
        # Rule: only sellers are gated by granular permissions
        # (deps.require_permission). A seller with none can log in and see
        # an empty panel, which reads to the user as "broken".
        if not hierarchy.role(a) == hierarchy.ROLE_SELLER:
            continue
        from ..permissions import effective_permissions
        if not effective_permissions(a):
            warn(f"فروشنده {_name(a)}: هیچ مجوزی ندارد - وارد می‌شود ولی پنلش خالی است")

    return {"by_id": by_id, "counts": counts}


def check_customers(db: Session, ctx: dict) -> None:
    by_id = ctx["by_id"]
    rows = db.query(models.User.id, models.User.username, models.User.owner_admin_id).all()
    orphan = [r for r in rows if r.owner_admin_id is None]
    dangling = [r for r in rows if r.owner_admin_id is not None and r.owner_admin_id not in by_id]

    print(f"مشتریان: {len(rows)}   بدون صاحب: {len(orphan)}   با صاحب ناموجود: {len(dangling)}")
    if orphan:
        # Rule: orphans are visible ONLY to a superadmin
        # (hierarchy.user_visibility_clause), so nobody else can fix them.
        warn(f"{len(orphan)} مشتری صاحب ندارند - فقط سوپرادمین آن‌ها را می‌بیند: "
             + ", ".join(r.username for r in orphan[:8]) + (" ..." if len(orphan) > 8 else ""))
    for r in dangling:
        bad(f"مشتری {r.username}: صاحبش (id={r.owner_admin_id}) در جدول ادمین‌ها نیست - در هیچ پنلی دیده نمی‌شود")


def check_ownership_rules(db: Session, ctx: dict) -> None:
    by_id = ctx["by_id"]
    sellers = {a.id for a in ctx["counts"][hierarchy.ROLE_SELLER]}

    # Rule: sellers never own packages (models.Package docstring,
    # hierarchy.accessible_package_owner_ids).
    for p in db.query(models.Package).filter(models.Package.owner_admin_id.isnot(None)).all():
        if p.owner_admin_id in sellers:
            bad(f"بسته «{p.name}»: مالکش {_name(by_id.get(p.owner_admin_id))} فروشنده است - "
                "فروشنده نباید مالک بسته باشد و این بسته در پنل او دیده نمی‌شود")
        elif p.owner_admin_id not in by_id:
            bad(f"بسته «{p.name}»: مالکش (id={p.owner_admin_id}) وجود ندارد")

    # Rule: a seller has NO node access at all
    # (hierarchy.accessible_node_ids returns an empty set for them).
    for n in db.query(models.Node).filter(models.Node.owner_admin_id.isnot(None)).all():
        if n.owner_admin_id in sellers:
            bad(f"نود «{n.name}»: مالکش فروشنده است - فروشنده اصلا نباید نود داشته باشد")
    granted_to_sellers: dict[int, int] = {}
    for g in db.query(models.AdminNodeAccess).all():
        if g.admin_id in sellers:
            granted_to_sellers[g.admin_id] = granted_to_sellers.get(g.admin_id, 0) + 1
    for admin_id, n in granted_to_sellers.items():
        bad(f"به فروشنده {_name(by_id.get(admin_id))} دسترسی {n} نود داده شده ولی کد برای فروشنده "
            "دسترسی نود را صفر می‌کند - یا نقش این حساب اشتباه است یا این دسترسی‌ها زائدند")

    # Rule: a seller price belongs to a seller, for a package inside their
    # parent's scope (routers/packages.py set_seller_price).
    for sp in db.query(models.PackageSellerPrice).all():
        seller = by_id.get(sp.seller_admin_id)
        if seller is None:
            bad(f"قیمت فروشنده (بسته {sp.package_id}): فروشنده id={sp.seller_admin_id} وجود ندارد")
            continue
        if hierarchy.role(seller) != hierarchy.ROLE_SELLER:
            warn(f"قیمت اختصاصی برای {_name(seller)} ثبت شده ولی او فروشنده نیست - اعمال نمی‌شود")
            continue
        pkg = db.get(models.Package, sp.package_id)
        if pkg is None:
            bad(f"قیمت فروشنده {_name(seller)}: بسته id={sp.package_id} حذف شده است")
        elif pkg.owner_admin_id != seller.parent_admin_id and pkg.owner_admin_id is not None:
            warn(f"قیمت فروشنده {_name(seller)} روی بسته «{pkg.name}» که مال والد او نیست - "
                 "احتمالا بعد از جابه‌جایی فروشنده جا مانده")


def check_bots(db: Session, ctx: dict) -> None:
    # Rule: Telegram allows exactly ONE getUpdates poller per token. Two
    # rows with the same token knock each other offline in a loop
    # (routers/telegram_bot_settings.py has a save-time guard; rows that
    # predate it are only caught at startup).
    seen: dict[str, list[str]] = {}
    shared = db.get(models.BotSettings, 1)
    if shared and (shared.bot_token or "").strip():
        seen.setdefault(shared.bot_token.strip(), []).append("ربات مشترک پنل")
    for a in ctx["by_id"].values():
        token = (a.own_bot_token or "").strip()
        if token:
            seen.setdefault(token, []).append(_name(a))
    for token, owners in seen.items():
        if len(owners) > 1:
            bad("یک توکن ربات بین این‌ها مشترک است و همدیگر را از کار می‌اندازند: " + " و ".join(owners))

    for a in ctx["counts"][hierarchy.ROLE_SELLER] + ctx["counts"][hierarchy.ROLE_ADMIN]:
        if (a.own_bot_token or "").strip() and a.telegram_id is None:
            warn(f"{_name(a)}: ربات اختصاصی دارد ولی آیدی تلگرامش وصل نشده - منوی مدیریت را نمی‌بیند")


def check_money(db: Session, ctx: dict) -> None:
    for a in ctx["by_id"].values():
        if hierarchy.role(a) == hierarchy.ROLE_SUPERADMIN:
            continue
        if (a.balance or 0) < 0:
            warn(f"{_name(a)}: موجودی منفی ({a.balance:,}) - بدهکار است")
        if a.billing_mode == "volume" and (a.volume_balance_gb or 0) < 0:
            warn(f"{_name(a)}: حجم باقی‌مانده منفی ({a.volume_balance_gb} GB)")


def main() -> int:
    db = SessionLocal()
    try:
        print("=" * 60)
        ctx = check_roles(db)
        print_tree(db, ctx)
        check_customers(db, ctx)
        check_ownership_rules(db, ctx)
        check_bots(db, ctx)
        check_money(db, ctx)
        print("=" * 60)
        if not findings:
            print("هیچ ناسازگاری‌ای پیدا نشد.")
            return 0
        for severity, msg in findings:
            print(f"[{severity}] {msg}")
        errors = sum(1 for s, _ in findings if s == "خطا")
        print("=" * 60)
        print(f"{errors} خطا و {len(findings) - errors} هشدار")
        # Exit code stays 0 even with findings: this is a report, and a
        # non-zero code would make it look like the script itself failed.
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
