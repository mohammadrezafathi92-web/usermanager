"""On-demand database health report ("بررسی سلامت دیتابیس" - Settings >
data tab, superadmin only): integrity, orphaned records, hierarchy
inconsistency.

Why this exists at all: SQLite (this project's default database - see
database.py) never enables `PRAGMA foreign_keys=ON`. Every ForeignKey()
declared in models.py is therefore metadata only - SQLAlchemy uses it to
generate the CREATE TABLE DDL and to build ORM relationships, but the
database itself never refuses an insert/update that points at a row which
doesn't exist, and never cascades a delete. A bug, a direct DB edit, or a
delete path that was written before a new related table existed can all
leave a dangling reference behind with no error at the time it happened -
it only surfaces later, as a confusing 500 or a customer who silently
vanished from every list. This module finds those after the fact.

Deliberately READ-ONLY. It reports; it does not repair anything - a
mismatched tree_path or an orphaned Connection can have more than one
correct fix depending on what actually happened, and guessing wrong here
would turn a visible, fixable problem into a silent, wrong "fix". Nothing
in routers/db_health.py has a POST for repairs; that stays a human
decision, made with the specific row in front of them.

Bounded on purpose: EVERY check loads at most a handful of whole tables'
worth of ids into memory (never full rows) and returns at most
EXAMPLES_PER_ISSUE example rows per finding - this runs on demand, from a
button in Settings, and must stay cheap even on the panel's biggest
installs (see the 20k-customer stress test elsewhere in this project).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .. import models
from . import hierarchy

EXAMPLES_PER_ISSUE = 20


@dataclass
class Issue:
    category: str
    severity: str  # "error" | "warning"
    title: str
    count: int
    examples: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "count": self.count,
            "examples": self.examples,
        }


def _missing_refs(db: Session, child_model, fk_col, parent_pk) -> list[tuple[int, int]]:
    """(child_id, dangling_fk_value) pairs for every row whose fk_col points
    at a parent_pk value that does not exist. Two id-only queries + a set
    difference - never a per-row lookup, never a full-row join."""
    parent_ids = {row[0] for row in db.query(parent_pk).all()}
    rows = db.query(child_model.id, fk_col).filter(fk_col.isnot(None)).all()
    return [(cid, fk) for cid, fk in rows if fk not in parent_ids]


def _orphan_issue(db: Session, *, category: str, severity: str, title_fmt: str,
                   child_model, fk_col, parent_pk, example_fields) -> Issue | None:
    missing = _missing_refs(db, child_model, fk_col, parent_pk)
    if not missing:
        return None
    examples = []
    if missing:
        ids = [cid for cid, _ in missing[:EXAMPLES_PER_ISSUE]]
        rows = db.query(child_model).filter(child_model.id.in_(ids)).all()
        by_id = {r.id: r for r in rows}
        for cid, fk in missing[:EXAMPLES_PER_ISSUE]:
            row = by_id.get(cid)
            examples.append({"id": cid, "dangling_ref": fk, **example_fields(row)})
    return Issue(category=category, severity=severity,
                 title=title_fmt.format(count=len(missing)), count=len(missing), examples=examples)


def _check_orphans(db: Session) -> list[Issue]:
    issues = []

    issues.append(_orphan_issue(
        db, category="orphan_user_owner", severity="warning",
        title_fmt="{count} کاربر با owner_admin_id به یک ادمین حذف‌شده اشاره می‌کنند",
        child_model=models.User, fk_col=models.User.owner_admin_id, parent_pk=models.AdminUser.id,
        example_fields=lambda r: {"username": r.username if r else None},
    ))
    issues.append(_orphan_issue(
        db, category="orphan_connection_user", severity="error",
        title_fmt="{count} اتصال به کاربری اشاره می‌کنند که وجود ندارد",
        child_model=models.Connection, fk_col=models.Connection.user_id, parent_pk=models.User.id,
        example_fields=lambda r: {"ppp_username": r.ppp_username if r else None},
    ))
    issues.append(_orphan_issue(
        db, category="orphan_connection_node", severity="error",
        title_fmt="{count} اتصال به نودی اشاره می‌کنند که حذف شده",
        child_model=models.Connection, fk_col=models.Connection.node_id, parent_pk=models.Node.id,
        example_fields=lambda r: {"ppp_username": r.ppp_username if r else None},
    ))
    issues.append(_orphan_issue(
        db, category="orphan_connection_purchase", severity="warning",
        title_fmt="{count} اتصال به یک خرید (Purchase) حذف‌شده اشاره می‌کنند",
        child_model=models.Connection, fk_col=models.Connection.purchase_id, parent_pk=models.Purchase.id,
        example_fields=lambda r: {"ppp_username": r.ppp_username if r else None},
    ))
    issues.append(_orphan_issue(
        db, category="orphan_purchase_user", severity="error",
        title_fmt="{count} خرید (Purchase) به کاربری اشاره می‌کنند که وجود ندارد",
        child_model=models.Purchase, fk_col=models.Purchase.user_id, parent_pk=models.User.id,
        example_fields=lambda r: {"package_name_snapshot": getattr(r, "package_name_snapshot", None)},
    ))
    issues.append(_orphan_issue(
        db, category="orphan_purchase_package", severity="warning",
        title_fmt="{count} خرید (Purchase) به پکیجی اشاره می‌کنند که حذف شده",
        child_model=models.Purchase, fk_col=models.Purchase.package_id, parent_pk=models.Package.id,
        example_fields=lambda r: {"user_id": r.user_id if r else None},
    ))
    issues.append(_orphan_issue(
        db, category="orphan_radius_active_session", severity="warning",
        title_fmt="{count} نشست فعال RADIUS به اتصالی اشاره می‌کنند که حذف شده",
        child_model=models.RadiusActiveSession, fk_col=models.RadiusActiveSession.connection_id,
        parent_pk=models.Connection.id,
        example_fields=lambda r: {"session_id": r.session_id if r else None},
    ))
    issues.append(_orphan_issue(
        db, category="orphan_admin_node_access_admin", severity="warning",
        title_fmt="{count} دسترسی نود به ادمینی اشاره می‌کند که حذف شده",
        child_model=models.AdminNodeAccess, fk_col=models.AdminNodeAccess.admin_id, parent_pk=models.AdminUser.id,
        example_fields=lambda r: {"node_id": r.node_id if r else None},
    ))
    issues.append(_orphan_issue(
        db, category="orphan_admin_node_access_node", severity="warning",
        title_fmt="{count} دسترسی نود به نودی اشاره می‌کند که حذف شده",
        child_model=models.AdminNodeAccess, fk_col=models.AdminNodeAccess.node_id, parent_pk=models.Node.id,
        example_fields=lambda r: {"admin_id": r.admin_id if r else None},
    ))
    issues.append(_orphan_issue(
        db, category="orphan_package_file", severity="warning",
        title_fmt="{count} فایل پکیج به پکیجی اشاره می‌کند که حذف شده",
        child_model=models.PackageFile, fk_col=models.PackageFile.package_id, parent_pk=models.Package.id,
        example_fields=lambda r: {"filename": getattr(r, "filename", None)},
    ))
    issues.append(_orphan_issue(
        db, category="orphan_package_ovpn_template", severity="warning",
        title_fmt="{count} قالب OVPN به پکیجی اشاره می‌کند که حذف شده",
        child_model=models.PackageOvpnTemplate, fk_col=models.PackageOvpnTemplate.package_id, parent_pk=models.Package.id,
        example_fields=lambda r: {"name": getattr(r, "name", None)},
    ))
    issues.append(_orphan_issue(
        db, category="orphan_package_connection", severity="warning",
        title_fmt="{count} سرویس پکیج-نود به پکیج/نودی اشاره می‌کنند که حذف شده",
        child_model=models.PackageConnection, fk_col=models.PackageConnection.package_id, parent_pk=models.Package.id,
        example_fields=lambda r: {"node_id": r.node_id if r else None},
    ))
    issues.append(_orphan_issue(
        db, category="orphan_package_seller_price", severity="warning",
        title_fmt="{count} قیمت اختصاصی فروشنده به پکیجی اشاره می‌کند که حذف شده",
        child_model=models.PackageSellerPrice, fk_col=models.PackageSellerPrice.package_id, parent_pk=models.Package.id,
        example_fields=lambda r: {"seller_admin_id": r.seller_admin_id if r else None},
    ))

    return [i for i in issues if i is not None]


def _check_duplicate_ppp_usernames(db: Session) -> Issue | None:
    """RADIUS auth (services/radius_server.py) looks a connection up by
    `.filter(Connection.ppp_username == username).first()` - a duplicate
    username means one of the two connections is simply unreachable by
    RADIUS forever (silently, since .first() never errors), even though
    both look fine in the panel."""
    from sqlalchemy import func

    rows = (
        db.query(models.Connection.ppp_username, func.count(models.Connection.id))
        .filter(models.Connection.ppp_username.isnot(None), models.Connection.ppp_username != "")
        .group_by(models.Connection.ppp_username)
        .having(func.count(models.Connection.id) > 1)
        .limit(EXAMPLES_PER_ISSUE)
        .all()
    )
    if not rows:
        return None
    # A second pass to get the total number of distinct duplicated usernames
    # (the LIMIT above is only for the examples shown).
    total_q = (
        db.query(models.Connection.ppp_username)
        .filter(models.Connection.ppp_username.isnot(None), models.Connection.ppp_username != "")
        .group_by(models.Connection.ppp_username)
        .having(func.count(models.Connection.id) > 1)
    )
    total = total_q.count()
    examples = [{"ppp_username": u, "connection_count": c} for u, c in rows]
    return Issue(
        category="duplicate_ppp_username", severity="error",
        title=f"{total} نام کاربری PPP روی چند اتصال تکرار شده - RADIUS فقط یکی را می‌بیند",
        count=total, examples=examples,
    )


def _check_negative_values(db: Session) -> list[Issue]:
    issues = []

    bad_purchases = (
        db.query(models.Purchase.id, models.Purchase.user_id)
        .filter((models.Purchase.used_bytes < 0) | (models.Purchase.quota_bytes < 0))
        .limit(EXAMPLES_PER_ISSUE)
        .all()
    )
    if bad_purchases:
        total = (
            db.query(models.Purchase.id)
            .filter((models.Purchase.used_bytes < 0) | (models.Purchase.quota_bytes < 0))
            .count()
        )
        issues.append(Issue(
            category="negative_purchase_usage", severity="error",
            title=f"{total} خرید (Purchase) مقدار مصرف یا حجم منفی دارند",
            count=total,
            examples=[{"id": pid, "user_id": uid} for pid, uid in bad_purchases],
        ))

    bad_users = (
        db.query(models.User.id, models.User.username)
        .filter((models.User.used_bytes < 0) | (models.User.total_quota_bytes < 0))
        .limit(EXAMPLES_PER_ISSUE)
        .all()
    )
    if bad_users:
        total = (
            db.query(models.User.id)
            .filter((models.User.used_bytes < 0) | (models.User.total_quota_bytes < 0))
            .count()
        )
        issues.append(Issue(
            category="negative_user_usage", severity="error",
            title=f"{total} کاربر مقدار مصرف یا حجم منفی دارند",
            count=total,
            examples=[{"id": uid, "username": uname} for uid, uname in bad_users],
        ))

    return issues


def _check_hierarchy(db: Session) -> list[Issue]:
    """Reuses hierarchy.py's own build_path/path_depth/validate_placement -
    the correct shape of a valid tree is defined there once; this only
    checks that every row actually matches it, rather than re-deriving the
    rules a second time in a way that could quietly drift from the real
    ones."""
    admins = db.query(models.AdminUser).all()
    by_id = {a.id: a for a in admins}

    orphaned_parent: list[dict] = []
    cycles: list[dict] = []
    path_mismatches: list[dict] = []
    depth_mismatches: list[dict] = []
    invalid_placement: list[dict] = []

    for a in admins:
        if a.parent_admin_id is not None and a.parent_admin_id not in by_id:
            orphaned_parent.append({"id": a.id, "username": a.username, "parent_admin_id": a.parent_admin_id})
            continue  # nothing further can be checked correctly without a real parent

        # Cycle detection - same guarded walk as main.py's own
        # _backfill_roles_and_paths.ancestor_count, so a corrupt loop can
        # never hang this check either.
        seen = {a.id}
        cur = a
        cycle_found = False
        while cur.parent_admin_id is not None:
            nxt = by_id.get(cur.parent_admin_id)
            if nxt is None:
                break
            if nxt.id in seen:
                cycle_found = True
                break
            seen.add(nxt.id)
            cur = nxt
        if cycle_found:
            cycles.append({"id": a.id, "username": a.username})
            continue  # path/placement checks are meaningless on a cycle

        parent = by_id.get(a.parent_admin_id) if a.parent_admin_id else None
        expected_path = hierarchy.build_path(parent.tree_path if parent else None, a.id)
        if (a.tree_path or "") != expected_path:
            path_mismatches.append({
                "id": a.id, "username": a.username,
                "stored": a.tree_path, "expected": expected_path,
            })

        expected_depth = hierarchy.path_depth(a.tree_path)
        if (a.depth or 0) != expected_depth:
            depth_mismatches.append({
                "id": a.id, "username": a.username,
                "stored": a.depth, "expected": expected_depth,
            })

        if not a.is_superadmin:
            reason = hierarchy.validate_placement(hierarchy.role(a), parent)
            if reason:
                invalid_placement.append({"id": a.id, "username": a.username, "reason": reason})

    issues = []
    if orphaned_parent:
        issues.append(Issue(
            category="hierarchy_orphaned_parent", severity="error",
            title=f"{len(orphaned_parent)} ادمین به یک والد حذف‌شده اشاره می‌کنند",
            count=len(orphaned_parent), examples=orphaned_parent[:EXAMPLES_PER_ISSUE],
        ))
    if cycles:
        issues.append(Issue(
            category="hierarchy_cycle", severity="error",
            title=f"{len(cycles)} ادمین در یک چرخه‌ی والد/فرزند گیر کرده‌اند",
            count=len(cycles), examples=cycles[:EXAMPLES_PER_ISSUE],
        ))
    if path_mismatches:
        issues.append(Issue(
            category="hierarchy_path_mismatch", severity="warning",
            title=f"{len(path_mismatches)} ادمین مسیر درخت (tree_path) نادرست ذخیره‌شده دارند",
            count=len(path_mismatches), examples=path_mismatches[:EXAMPLES_PER_ISSUE],
        ))
    if depth_mismatches:
        issues.append(Issue(
            category="hierarchy_depth_mismatch", severity="warning",
            title=f"{len(depth_mismatches)} ادمین عمق (depth) نادرست ذخیره‌شده دارند",
            count=len(depth_mismatches), examples=depth_mismatches[:EXAMPLES_PER_ISSUE],
        ))
    if invalid_placement:
        issues.append(Issue(
            category="hierarchy_invalid_placement", severity="warning",
            title=f"{len(invalid_placement)} ادمین در جایگاهی هستند که با قوانین سلسله‌مراتب جور نیست",
            count=len(invalid_placement), examples=invalid_placement[:EXAMPLES_PER_ISSUE],
        ))

    return issues


def run_health_check(db: Session) -> dict:
    issues: list[Issue] = []
    issues.extend(_check_hierarchy(db))
    issues.extend(_check_orphans(db))
    dup = _check_duplicate_ppp_usernames(db)
    if dup:
        issues.append(dup)
    issues.extend(_check_negative_values(db))

    error_count = sum(i.count for i in issues if i.severity == "error")
    warning_count = sum(i.count for i in issues if i.severity == "warning")

    return {
        "checked_at": dt.datetime.utcnow().isoformat(),
        "healthy": not issues,
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": [i.to_dict() for i in issues],
    }
