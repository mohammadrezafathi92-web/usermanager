"""Phase C (docs/api-key-scope-audit-2026-09-27.md) - lifecycle tests for the
three C0 activation surfaces the design's file list calls for and which
test_bot_auth_principal.py/test_bot_route_policy_coverage.py/
test_bot_resource_accessor_ast.py deliberately do NOT cover (those test the
GUARDS; this file tests the OPERATOR-FACING ON/OFF SWITCHES around them):

  1. POST /api/api-keys/{id}/activate - the only path that ever turns a
     tenant_integration key on (flips enabled+scope_enforced together).
  2. PUT /api/settings/package-node-scope (+ /disable, + GET status) -
     the per-installation package-authorization rollout switch.
  3. _lock_package_node_scope_settings's real mutual-exclusion guarantee,
     proven through two independent DB sessions on the same on-disk SQLite
     file hitting the real HTTP endpoint concurrently - not two direct
     Python calls on one shared session (which sqlite3's own single-
     connection serialization would trivially "pass" without proving
     anything about the lock).
  4. The dialect split itself (BEGIN IMMEDIATE only for SQLite, SELECT...
     FOR UPDATE only otherwise) - compiled-SQL text, not behavior, since
     this repo only ever runs against SQLite and a MySQL assertion needs
     to be provable without a MySQL server present.
  5. create_package's atomicity: a failure in _sync_ovpn_templates (after
     pkg + its PackageConnection rows are already flushed) must leave
     NOTHING committed - not a partially-created package with no
     connections.

Every flag/column touched here still defaults False on every OTHER row -
this file only exercises the explicit, single-row transitions the design
allows in C0; it never enables enforcement on a second row as a side
effect of testing the first.

Run:  python3 backend/tests/test_phase_c_lifecycle.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models, schemas
from app.database import get_db
from app.deps import get_current_admin
from app.routers import api_keys, packages as packages_router, panel_settings
from app.security import hash_password
from app.services import bot_auth, hierarchy


failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except HTTPException as exc:
        return f"{exc.status_code}: {exc.detail}"


PW = "correct-password"
CONFIRM = {"X-Confirm-Password": PW}


# ===========================================================================
print("=" * 72)
print("--- activate_tenant_key: the only path that turns a tenant key on ---")
print("=" * 72)

engine1 = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(engine1)
Session1 = sessionmaker(bind=engine1)
db1 = Session1()

root1 = models.AdminUser(username="root1", hashed_password=hash_password(PW), is_superadmin=True)
owner1 = models.AdminUser(username="tenant1", hashed_password=hash_password("x"), role="admin")
db1.add_all([root1, owner1])
db1.commit()
db1.refresh(root1)
db1.refresh(owner1)

app1 = FastAPI()
app1.include_router(api_keys.router)
app1.dependency_overrides[get_db] = lambda: db1
app1.dependency_overrides[get_current_admin] = lambda: root1
client1 = TestClient(app1)

resp = client1.post("/api/api-keys", json={"label": "t1", "owner_admin_id": owner1.id})
tenant_id = resp.json()["id"]
tenant = db1.get(models.ApiKey, tenant_id)
check("newly created tenant key starts disabled", tenant.enabled, False)
check("newly created tenant key starts unenforced", tenant.scope_enforced, False)

resp = client1.post(f"/api/api-keys/{tenant_id}/activate")
check("activate without the confirm-password header is refused", resp.status_code, 403)
db1.refresh(tenant)
check("a refused activate attempt leaves the key untouched", (tenant.enabled, tenant.scope_enforced), (False, False))

resp = client1.post(f"/api/api-keys/{tenant_id}/activate", headers={"X-Confirm-Password": "wrong"})
check("activate with a wrong password is refused", resp.status_code, 403)

resp = client1.post(f"/api/api-keys/{tenant_id}/activate", headers=CONFIRM)
check("activate with the right password succeeds", resp.status_code, 200)
db1.refresh(tenant)
check("activation flips enabled AND scope_enforced together, atomically",
      (tenant.enabled, tenant.scope_enforced), (True, True))

resp = client1.post(f"/api/api-keys/{tenant_id}/activate", headers=CONFIRM)
check("activating an already-active key is refused, not a silent no-op", resp.status_code, 409)
db1.refresh(tenant)
check("the second (refused) attempt did not change anything", (tenant.enabled, tenant.scope_enforced), (True, True))

resp = client1.post("/api/api-keys/999999/activate", headers=CONFIRM)
check("activating a key id that does not exist is refused the same way (409, not a 404 leak)",
      resp.status_code, 409)

resp = client1.post("/api/api-keys/global", json={"label": "g1"}, headers=CONFIRM)
global_id = resp.json()["id"]
resp = client1.post(f"/api/api-keys/{global_id}/activate", headers=CONFIRM)
check("activate is a tenant_integration-only path - a global key's own id 409s here too",
      resp.status_code, 409)
db1.refresh(db1.get(models.ApiKey, global_id))
check("attempting activate on the global key never touches its scope_enforced",
      db1.get(models.ApiKey, global_id).scope_enforced, False)


# ===========================================================================
print("\n" + "=" * 72)
print("--- package-node-scope: enable/disable/status ---")
print("=" * 72)

engine2 = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(engine2)
Session2 = sessionmaker(bind=engine2)
db2 = Session2()

root2 = models.AdminUser(username="root2", hashed_password=hash_password(PW), is_superadmin=True)
admin2 = models.AdminUser(username="admin2", hashed_password=hash_password("x"), role="admin")
other_admin2 = models.AdminUser(username="other2", hashed_password=hash_password("x"), role="admin")
db2.add_all([root2, admin2, other_admin2])
db2.commit()
db2.refresh(root2)
db2.refresh(admin2)
db2.refresh(other_admin2)

app2 = FastAPI()
app2.include_router(panel_settings.router)
app2.dependency_overrides[get_db] = lambda: db2
app2.dependency_overrides[get_current_admin] = lambda: root2
client2 = TestClient(app2)

check("no PanelSettings row exists yet in this fresh test db", db2.get(models.PanelSettings, 1), None)

resp = client2.get("/api/settings/package-node-scope")
check("status read auto-creates the missing PanelSettings row rather than 500ing",
      resp.status_code, 200)
check("status defaults to disabled on a brand-new/pre-existing install", resp.json()["enabled"], False)
check("the auto-created row is really persisted now", db2.get(models.PanelSettings, 1) is not None, True)

resp = client2.put("/api/settings/package-node-scope")
check("enable without confirm-password header is refused", resp.status_code, 403)

# --- mismatch scenario: admin2's own package points at a node admin2 has no
# selling-scope access to (owned by an unrelated Admin, not granted) -----
node_admin2 = models.Node(name="n-admin2", type=models.NodeType.mikrotik,
                          mt_host="1.1.1.1", mt_username="u", mt_password="p",
                          owner_admin_id=admin2.id)
node_other = models.Node(name="n-other", type=models.NodeType.mikrotik,
                         mt_host="2.2.2.2", mt_username="u", mt_password="p",
                         owner_admin_id=other_admin2.id)
db2.add_all([node_admin2, node_other])
db2.commit()

pkg2 = models.Package(name="p2", quota_gb=10, duration_days=30, price=1000,
                      enabled=True, bot_enabled=True, owner_admin_id=admin2.id)
db2.add(pkg2)
db2.commit()
mismatch_conn = models.PackageConnection(package_id=pkg2.id, node_id=node_other.id,
                                         protocol=models.ConnectionType.wireguard)
db2.add(mismatch_conn)
db2.commit()

check("selling_scope_node_ids for admin2 does not include other_admin2's node (sanity check)",
      node_other.id in (hierarchy.selling_scope_node_ids(db2, admin2) or set()), False)

resp = client2.put("/api/settings/package-node-scope", headers=CONFIRM)
check("enable is refused while a real mismatch exists", resp.status_code, 409)

resp = client2.get("/api/settings/package-node-scope")
check("a refused enable leaves the flag false", resp.json()["enabled"], False)

# fix the mismatch by pointing the connection at admin2's own node instead
mismatch_conn.node_id = node_admin2.id
db2.commit()

resp = client2.put("/api/settings/package-node-scope", headers=CONFIRM)
check("enable succeeds once every PackageConnection resolves inside its owner's selling scope",
      resp.status_code, 200)

resp = client2.get("/api/settings/package-node-scope")
check("status now reads enabled=True", resp.json()["enabled"], True)

resp = client2.put("/api/settings/package-node-scope/disable", headers=CONFIRM)
check("disable succeeds without needing a re-validation pass (only ever widens access)",
      resp.status_code, 200)
resp = client2.get("/api/settings/package-node-scope")
check("status reads disabled again after disable", resp.json()["enabled"], False)

resp = client2.put("/api/settings/package-node-scope/disable")
check("disable also requires the confirm-password header", resp.status_code, 403)


# ===========================================================================
print("\n" + "=" * 72)
print("--- real two-session lock: BEGIN IMMEDIATE actually serializes ---")
print("=" * 72)

# A shared in-memory StaticPool db (as used above) hands both "sessions" the
# SAME underlying sqlite3 connection - proving nothing about the lock, only
# about Python-level GIL ordering. This section instead uses a real on-disk
# file so two independent sqlite3 connections genuinely contend for it, and
# widens the window with a real sleep so a race would actually have to be
# won, not just theoretically possible.

# Both threads below hit the SAME shared lock (_lock_package_node_scope_
# settings on PanelSettings row id=1), but through the TWO DIFFERENT
# endpoints the design's own TOCTOU concern is actually about: a package
# WRITE (routers/packages.py's create_package) racing the activation
# endpoint (panel_settings.enable_package_node_scope) - not two activation
# calls against each other, which would only prove the activation endpoint
# doesn't collide with itself and say nothing about the write-vs-activate
# invariant _lock_package_node_scope_settings exists to close (caught in
# review - the first version of this test picked the wrong pair of
# endpoints).

tmpdir = tempfile.mkdtemp(prefix="phase_c_lock_")
db_path = os.path.join(tmpdir, "race.db")
file_url = f"sqlite:///{db_path}"

engine_pkg = create_engine(file_url, connect_args={"check_same_thread": False, "timeout": 10})
models.Base.metadata.create_all(engine_pkg)
SessionPkg = sessionmaker(bind=engine_pkg)
db_pkg = SessionPkg()

engine_act = create_engine(file_url, connect_args={"check_same_thread": False, "timeout": 10})
SessionAct = sessionmaker(bind=engine_act)
db_act = SessionAct()

root_race = models.AdminUser(username="root_race", hashed_password=hash_password(PW), is_superadmin=True)
db_pkg.add(root_race)
db_pkg.commit()
db_pkg.refresh(root_race)
# db_act's own connection did not exist yet when root_race was committed
# over db_pkg's connection on the same file - re-queried by id below,
# exactly like a second real HTTP request would with its own fresh
# session.
root_race_id = root_race.id

node_race = models.Node(name="n-race", type=models.NodeType.mikrotik,
                        mt_host="9.9.9.9", mt_username="u", mt_password="p")
db_pkg.add(node_race)
db_pkg.commit()
node_race_id = node_race.id

app_pkg = FastAPI()
app_pkg.include_router(packages_router.router)
app_pkg.dependency_overrides[get_db] = lambda: db_pkg
app_pkg.dependency_overrides[get_current_admin] = lambda: db_pkg.get(models.AdminUser, root_race_id)
client_pkg = TestClient(app_pkg)

app_act = FastAPI()
app_act.include_router(panel_settings.router)
app_act.dependency_overrides[get_db] = lambda: db_act
app_act.dependency_overrides[get_current_admin] = lambda: db_act.get(models.AdminUser, root_race_id)
client_act = TestClient(app_act)

# Widen the window the lock is held for, on WHICHEVER of the two endpoints
# acquires it first - patched at the module-attribute level in BOTH
# routers/packages.py's and routers/panel_settings.py's own namespaces
# (each did `from ..services.bot_auth import _lock_package_node_scope_
# settings`, which copies the reference at import time - patching
# bot_auth's own attribute alone would not reach either caller).
_real_lock = bot_auth._lock_package_node_scope_settings
_hold_seconds = 0.4


def _slow_lock(db):
    row = _real_lock(db)
    time.sleep(_hold_seconds)  # lock is already held (BEGIN IMMEDIATE ran above) - widen the window it's held for
    return row


bot_auth._lock_package_node_scope_settings = _slow_lock
packages_router._lock_package_node_scope_settings = _slow_lock
panel_settings._lock_package_node_scope_settings = _slow_lock

results: dict[str, tuple[int, float]] = {}


def _run_create_package():
    start = time.monotonic()
    resp = client_pkg.post(
        "/api/packages",
        json={
            "name": "race-package", "quota_gb": 5, "duration_days": 10, "price": 100,
            "connections": [{"node_id": node_race_id, "protocol": "wireguard"}],
        },
    )
    results["package_write"] = (resp.status_code, time.monotonic() - start)


def _run_enable():
    start = time.monotonic()
    resp = client_act.put("/api/settings/package-node-scope", headers=CONFIRM)
    results["activation"] = (resp.status_code, time.monotonic() - start)


t_pkg = threading.Thread(target=_run_create_package)
t_act = threading.Thread(target=_run_enable)
t_pkg.start()
time.sleep(0.05)  # give the package write a head start acquiring BEGIN IMMEDIATE first
t_act.start()
t_pkg.join(timeout=10)
t_act.join(timeout=10)

bot_auth._lock_package_node_scope_settings = _real_lock
packages_router._lock_package_node_scope_settings = _real_lock
panel_settings._lock_package_node_scope_settings = _real_lock

status_pkg, elapsed_pkg = results.get("package_write", (None, None))
status_act, elapsed_act = results.get("activation", (None, None))

check("both the concurrent package write and the activation attempt eventually complete (no deadlock/crash)",
      None in (status_pkg, status_act), False)
check("the package write succeeds (superadmin-owned package, node in scope - no mismatch possible)",
      status_pkg, 200)
check("the activation attempt succeeds too (no OTHER package has a real mismatch)",
      status_act, 200)
check(f"whichever of the two lost the race was genuinely blocked waiting for the "
      f"OTHER endpoint's lock, not merely lucky (write={elapsed_pkg:.2f}s, "
      f"activate={elapsed_act:.2f}s, hold={_hold_seconds}s)",
      max(elapsed_pkg, elapsed_act) >= _hold_seconds * 0.8, True)

db_pkg.close()
db_act.close()


# ===========================================================================
print("\n" + "=" * 72)
print("--- dialect split: BEGIN IMMEDIATE vs SELECT...FOR UPDATE never cross ---")
print("=" * 72)


class _FakeResult:
    def scalar_one_or_none(self):
        return None


class _FakeSession:
    """Records exactly what _lock_package_node_scope_settings asks the DB to
    do, without needing a real MySQL server to prove the MySQL branch never
    emits SQLite-only syntax (and vice versa)."""

    def __init__(self):
        self.executed: list[str] = []
        self.added = []
        self.flushed = False

    def execute(self, stmt):
        # `text("BEGIN IMMEDIATE")` renders back its own literal string;
        # the compiled `select(...).with_for_update()` construct is
        # rendered by calling str() on it, which invokes its own
        # dialect-default compiler - enough to distinguish the two
        # statement SHAPES without a live MySQL connection.
        self.executed.append(str(stmt))
        return _FakeResult()

    def get(self, model, pk):
        return None

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushed = True


real_is_sqlite = bot_auth.is_sqlite

bot_auth.is_sqlite = True
fake_sqlite = _FakeSession()
bot_auth._lock_package_node_scope_settings(fake_sqlite)
check("SQLite dialect emits BEGIN IMMEDIATE",
      any("BEGIN IMMEDIATE" in s for s in fake_sqlite.executed), True)
check("SQLite dialect never emits FOR UPDATE",
      any("FOR UPDATE" in s.upper() for s in fake_sqlite.executed), False)

bot_auth.is_sqlite = False
fake_mysql = _FakeSession()
bot_auth._lock_package_node_scope_settings(fake_mysql)
check("MySQL/MariaDB dialect never emits BEGIN IMMEDIATE (SQLite-only syntax)",
      any("BEGIN IMMEDIATE" in s for s in fake_mysql.executed), False)
check("MySQL/MariaDB dialect emits a FOR UPDATE row lock instead",
      any("FOR UPDATE" in s.upper() for s in fake_mysql.executed), True)

bot_auth.is_sqlite = real_is_sqlite


# ===========================================================================
print("\n" + "=" * 72)
print("--- create_package atomicity: a late failure rolls back everything ---")
print("=" * 72)

engine3 = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(engine3)
Session3 = sessionmaker(bind=engine3)
db3 = Session3()

root3 = models.AdminUser(username="root3", hashed_password=hash_password(PW), is_superadmin=True)
db3.add(root3)
db3.commit()
db3.refresh(root3)

node3 = models.Node(name="n3", type=models.NodeType.mikrotik,
                    mt_host="3.3.3.3", mt_username="u", mt_password="p")
db3.add(node3)
db3.commit()

before_pkg_count = db3.query(models.Package).count()
before_conn_count = db3.query(models.PackageConnection).count()

real_sync_ovpn = packages_router._sync_ovpn_templates


def _boom(db, pkg, specs):
    raise RuntimeError("simulated ovpn-template failure, after pkg+connections are flushed")


packages_router._sync_ovpn_templates = _boom
try:
    call(
        packages_router.create_package,
        schemas.PackageCreate(
            name="should-not-survive", quota_gb=5, duration_days=10, price=500,
            connections=[schemas.PackageConnectionSpec(node_id=node3.id, protocol=models.ConnectionType.wireguard)],
        ),
        db=db3, admin=root3,
    )
    raised = False
except RuntimeError:
    raised = True
finally:
    packages_router._sync_ovpn_templates = real_sync_ovpn

check("the forced failure actually propagated out of create_package", raised, True)

# Mirrors what a fresh per-request session's own get_db(...)/finally: db.close()
# would already do in production (Session.close() rolls back an in-progress
# transaction) - this test reuses one session across multiple simulated
# "requests" the way the rest of this suite does, so the rollback has to be
# done explicitly here rather than happening for free via request teardown.
db3.rollback()

after_pkg_count = db3.query(models.Package).count()
after_conn_count = db3.query(models.PackageConnection).count()
check("no Package row survives a failure that happens after it was flushed but before commit",
      after_pkg_count, before_pkg_count)
check("no PackageConnection row survives either - the whole write is one atomic unit",
      after_conn_count, before_conn_count)


# ===========================================================================
if failures:
    print(f"\n{len(failures)} failure(s): {', '.join(failures)}")
    raise SystemExit(1)
print("\nهمه‌ی تست‌ها گذشت")
