"""services/db_health.py: does the report actually find what's broken, and
stay quiet on a clean database?

Run:  python3 backend/tests/test_db_health.py

Every check is READ-ONLY (see the module's own docstring for why), so this
only verifies detection, never any kind of repair.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import db_health

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def fresh_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def issues_by_category(report):
    return {i["category"]: i for i in report["issues"]}


print("--- a clean, healthy database reports no issues at all ---")
db = fresh_db()
admin = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, role="superadmin", tree_path="/1/", depth=0)
db.add(admin)
db.commit()

node = models.Node(name="n1", type="mikrotik", enabled=True, mt_host="10.0.0.1")
pkg = models.Package(name="p1", quota_gb=50, price=1000)
db.add_all([node, pkg])
db.commit()

user = models.User(username="healthy_user", owner_admin_id=admin.id, total_quota_bytes=0, used_bytes=0)
db.add(user)
db.commit()

conn = models.Connection(user_id=user.id, node_id=node.id, type="l2tp",
                          ppp_username="healthy_user_c", ppp_password="x", enabled=True)
db.add(conn)
db.commit()

purchase = models.Purchase(user_id=user.id, package_id=pkg.id, quota_bytes=50, used_bytes=0)
db.add(purchase)
db.commit()

report = db_health.run_health_check(db)
check("reports healthy", report["healthy"], True)
check("no issues found", report["issues"], [])
check("zero errors", report["error_count"], 0)
check("zero warnings", report["warning_count"], 0)


print("\n--- orphaned Connection.user_id is caught ---")
db = fresh_db()
node = models.Node(name="n1", type="mikrotik", enabled=True, mt_host="10.0.0.1")
db.add(node)
db.commit()
bad_conn = models.Connection(user_id=99999, node_id=node.id, type="l2tp",
                              ppp_username="ghost_c", ppp_password="x", enabled=True)
db.add(bad_conn)
db.commit()
report = db_health.run_health_check(db)
issues = issues_by_category(report)
check("flags it", "orphan_connection_user" in issues, True)
check("with the right count", issues["orphan_connection_user"]["count"], 1)
check("...and a useful example", issues["orphan_connection_user"]["examples"][0]["ppp_username"], "ghost_c")
check("severity is error - RADIUS/panel code can crash walking .user", issues["orphan_connection_user"]["severity"], "error")


print("\n--- orphaned Purchase.user_id is caught ---")
db = fresh_db()
pkg = models.Package(name="p1", quota_gb=50, price=1000)
db.add(pkg)
db.commit()
db.add(models.Purchase(user_id=88888, package_id=pkg.id, quota_bytes=50, used_bytes=0))
db.commit()
report = db_health.run_health_check(db)
issues = issues_by_category(report)
check("flags it", issues.get("orphan_purchase_user", {}).get("count"), 1)


print("\n--- duplicate ppp_username: RADIUS's own .first() trap ---")
db = fresh_db()
admin = models.AdminUser(username="a", hashed_password="x", is_superadmin=True)
node = models.Node(name="n1", type="mikrotik", enabled=True, mt_host="10.0.0.1")
db.add_all([admin, node])
db.commit()
u1 = models.User(username="u1", owner_admin_id=admin.id)
u2 = models.User(username="u2", owner_admin_id=admin.id)
db.add_all([u1, u2])
db.commit()
db.add_all([
    models.Connection(user_id=u1.id, node_id=node.id, type="l2tp", ppp_username="shared_name", ppp_password="x", enabled=True),
    models.Connection(user_id=u2.id, node_id=node.id, type="l2tp", ppp_username="shared_name", ppp_password="y", enabled=True),
])
db.commit()
report = db_health.run_health_check(db)
issues = issues_by_category(report)
check("flags the duplicate", issues.get("duplicate_ppp_username", {}).get("count"), 1)
check("names the username", issues["duplicate_ppp_username"]["examples"][0]["ppp_username"], "shared_name")
check("...with the real count of connections sharing it",
      issues["duplicate_ppp_username"]["examples"][0]["connection_count"], 2)


print("\n--- negative usage/quota values are caught ---")
db = fresh_db()
admin = models.AdminUser(username="a", hashed_password="x", is_superadmin=True)
db.add(admin)
db.commit()
db.add(models.User(username="neg_user", owner_admin_id=admin.id, total_quota_bytes=10, used_bytes=-5))
db.commit()
report = db_health.run_health_check(db)
issues = issues_by_category(report)
check("flags negative user usage", issues.get("negative_user_usage", {}).get("count"), 1)


print("\n--- hierarchy: a cycle in parent_admin_id is caught, not an infinite loop ---")
db = fresh_db()
a = models.AdminUser(username="a", hashed_password="x", role="admin", tree_path="/1/", depth=0)
b = models.AdminUser(username="b", hashed_password="x", role="admin", tree_path="/1/2/", depth=1)
db.add_all([a, b])
db.commit()
a.parent_admin_id = b.id
b.parent_admin_id = a.id
db.commit()

import signal


def _timeout(_sig, _frame):
    raise TimeoutError("run_health_check hung - likely an infinite loop on the cycle")


old_handler = signal.signal(signal.SIGALRM, _timeout)
signal.alarm(5)
try:
    report = db_health.run_health_check(db)
finally:
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old_handler)

issues = issues_by_category(report)
check("finishes and flags the cycle", issues.get("hierarchy_cycle", {}).get("count"), 2)


print("\n--- hierarchy: a wrong stored tree_path is caught ---")
db = fresh_db()
root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, tree_path="/1/", depth=0)
db.add(root)
db.commit()
child = models.AdminUser(username="child", hashed_password="x", role="admin",
                          parent_admin_id=root.id, tree_path="/wrong/path/", depth=0)
db.add(child)
db.commit()
report = db_health.run_health_check(db)
issues = issues_by_category(report)
check("flags the mismatch", issues.get("hierarchy_path_mismatch", {}).get("count"), 1)
check("shows both stored and expected", issues["hierarchy_path_mismatch"]["examples"][0]["stored"], "/wrong/path/")


print("\n--- hierarchy: an orphaned parent_admin_id is caught (not crashed on) ---")
db = fresh_db()
db.add(models.AdminUser(username="orphan_admin", hashed_password="x", role="seller",
                         parent_admin_id=77777, tree_path="/77777/1/", depth=1))
db.commit()
report = db_health.run_health_check(db)
issues = issues_by_category(report)
check("flags it", issues.get("hierarchy_orphaned_parent", {}).get("count"), 1)


print("\n--- hierarchy: an invalid placement (seller with no parent) is caught ---")
db = fresh_db()
db.add(models.AdminUser(username="parentless_seller", hashed_password="x", role="seller",
                         tree_path="/1/", depth=0))
db.commit()
report = db_health.run_health_check(db)
issues = issues_by_category(report)
check("flags it", issues.get("hierarchy_invalid_placement", {}).get("count"), 1)


print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
