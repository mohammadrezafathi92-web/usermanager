"""ANALYSIS ONLY (per Product's own instruction for this stage) - reproduces
findings for the "چرخه کامل lifecycle و provisioning سرویس" review, before
any design is approved or any production code is touched. No fix lands in
this file or anywhere else this run.

Round 3 state (after three rounds of independent review):

  CONFIRMED, with real behavioral reproduction:
    [A]/[A2]  create_user's manual connections loop has no per-connection
              error containment - a mid-loop failure leaves an already-
              committed, unrecoverable-by-retry phantom User+Connection.
    [B]/[B2]  delete_user_cascade's per-connection deprovision loop has the
              same gap in the DELETE direction - a partial failure leaves
              DB and remote genuinely out of sync; retry self-heals ONLY
              because the mocked remote removal is idempotent (real
              idempotency across WireGuard/Xray SSH/3X-UI/SoftEther is
              still unverified - see the note on [B2] below).
    [C]/[C2]  apply_package_as_purchase tolerates partial bundled-
              connection fulfillment but still charges/delivers the FULL
              package price - verified with a REAL non-superadmin admin's
              balance/Ledger on both the bot and panel paths (round 2's
              own version of this check used a superadmin, who is never
              charged, and proved nothing about money).
    [D1-D3]   WireGuard/Xray/SoftEther all make their remote call BEFORE
              their own Connection row's db.commit() - reproduced
              independently per backend by failing exactly that commit
              after a successful (mocked) remote call. This ONLY proves
              the commit-failure case; a process crash (not a catchable
              Python exception) is a DIFFERENT failure class that no
              try/except here - or in any real fix - can address by
              itself. Durable operation/intent tracking is still an open
              design question, not something this file claims to solve.
    [E]       routers/users.py's single-user create_user charges the
              admin's wallet (which commits internally) BEFORE the User
              row's own commit, with no refund covering that window.
    [G1]/[G2] the bot's REVERSED ordering (charge AFTER create/purchase,
              not before) is not safe either - a later charge failure
              still leaves an already-committed, unbilled, fully
              provisioned User/Purchase/Connection. Round 2's own
              conclusion that bot signups "do NOT have finding E's
              ordering" was true but incomplete - it never checked what a
              LATER charge failure does to what already committed.
    [H]       bulk_create_users: a batch-level refund does not undo users
              already committed before the crash. Round 2's own version
              of this only checked the FIRST (fully provisioned) user;
              round 3 corrected it to also assert the SECOND (crashed-
              mid-provisioning) user's own half-built, Connection-less,
              Purchase-less phantom state - refund cleanup has to cover
              BOTH shapes, not just the "fully succeeded before the
              crash" one.
    [F-race]  a REAL two-session, barrier-synchronized race proves
              one_time_per_user's "only once" invariant is violated when
              both requests race the SAME existing User - no unique
              constraint or lock exists to prevent it.
    [F-race2] the SAME invariant, for the actual enforcement boundary the
              code claims (see _ensure_one_time_package_not_reused's own
              telegram_id-wide check) - two DIFFERENT usernames sharing
              ONE telegram_id, both racing POST /api/bot/users for the
              same one-time package. A fix that only locks on
              (user_id, package_id) would leave this exact cross-account
              path open.

  CHARACTERIZATION, not confirmed findings:
    [F]/[F2]  two sequential identical purchase-package/apply-package
              calls both succeed - true, but for an ORDINARY (non-one-
              time) package this may be entirely legitimate product
              behavior (Purchase exists so a customer can hold several
              independent services). Proves duplicates are POSSIBLE, not
              that any given pair of requests was unintended.

  Explicit, deliberate scope limits (not yet analyzed, not claimed done):
    - Every race/lock reproduction in this file runs on SQLite only. The
      codebase also officially supports MySQL/MariaDB (see database.py's
      is_sqlite/is_mysql and Phase C's own dialect-split precedent in
      bot_auth.py's _lock_package_node_scope_settings). No MariaDB-backed
      reproduction has been run. A real fix cannot rely on SQLite-only
      mechanics (e.g. BEGIN IMMEDIATE) and must give the same invariant
      on both dialects - and cannot rely on a bare unique constraint on
      (user_id, package_id) either, since [F-race2] shows the real
      constraint is telegram_id-wide, cross-account, while an ordinary
      (non-one-time) package must still allow being bought more than once.
    - [B2]'s retry only proves idempotency against a MOCK that was built
      to behave idempotently. The real idempotency of WireGuard peer
      removal, Xray-over-SSH client removal, 3X-UI's own API, and
      SoftEther's JSON-RPC removal are each still unverified against the
      real client code.
    - This file makes zero real network/socket calls (see the
      get_connection_share patch right after the node fixtures below) -
      it is a test_*.py picked up by run_all.py/CI, and round 3 found it
      was attempting genuine MikroTik connections during response
      serialization (WireGuard connections' config text needs the
      server's public key - see get_connection_share's own wireguard
      branch) even though provisioning itself was already mocked.

Run:  python3 backend/tests/test_lifecycle_provisioning_analysis.py
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

# ---------------------------------------------------------------------------
# Permanent, fail-closed network guard. A missing WARNING in one run is not
# a guarantee - if a later edit to this file (or to the production code it
# exercises) adds a new code path that reaches out to a real socket, this
# must turn into a loud, immediate test failure, not a silent slow timeout
# that only shows up as a suspiciously long CI run. Installed BEFORE any
# engine/fixture/app is built below, so nothing in this file can ever reach
# a real socket, by construction, not by convention. sqlite3 itself never
# touches the socket module (plain file I/O), and Starlette's TestClient
# (httpx + ASGITransport) calls the ASGI app in-process without opening a
# real connection either - so blocking these at the socket layer costs this
# file nothing while making "no network" an enforced invariant, not an
# observation. Also blocks sendto - this project has a real RADIUS/UDP
# server (services/radius_server.py), so a TCP-only guard would leave a
# genuine gap for anything that ever sent a UDP packet instead of opening a
# connect()ed socket.
class NetworkCallBlocked(AssertionError):
    pass


def _blocked_connect(self, address, *a, **k):
    raise NetworkCallBlocked(
        f"test_lifecycle_provisioning_analysis.py: blocked a REAL socket.connect to "
        f"{address!r} - this file must make ZERO real network calls. Whatever code path "
        f"just tried to reach out needs to be mocked, not exempted from this guard."
    )


def _blocked_create_connection(address, *a, **k):
    raise NetworkCallBlocked(
        f"test_lifecycle_provisioning_analysis.py: blocked a REAL socket.create_connection "
        f"to {address!r} - see _blocked_connect's own message."
    )


def _blocked_connect_ex(self, address, *a, **k):
    raise NetworkCallBlocked(
        f"test_lifecycle_provisioning_analysis.py: blocked a REAL socket.connect_ex to "
        f"{address!r} - see _blocked_connect's own message."
    )


def _blocked_sendto(self, *args, **kwargs):
    # sendto(data, address) or sendto(data, flags, address) - the
    # destination is whichever positional argument comes last; fall back to
    # a kwarg or "?" rather than raising a confusing TypeError out of the
    # guard itself if some exotic call shape ever reaches here.
    dest = args[-1] if args else kwargs.get("address", "?")
    raise NetworkCallBlocked(
        f"test_lifecycle_provisioning_analysis.py: blocked a REAL socket.sendto to "
        f"{dest!r} - see _blocked_connect's own message (this project has a real RADIUS/"
        f"UDP server, so UDP needs the same guard TCP connect/connect_ex already got)."
    )


socket.socket.connect = _blocked_connect
socket.create_connection = _blocked_create_connection
socket.socket.connect_ex = _blocked_connect_ex
socket.socket.sendto = _blocked_sendto
# ---------------------------------------------------------------------------

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import get_db
from app.deps import get_bot_principal, get_current_admin
from app.routers import bot as bot_router
from app.routers import users as users_router
from app.services import admin_billing, hierarchy, user_ops
from app.services.bot_auth import BotPrincipal

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


print("=" * 72)
print("--- [guard] negative control: the network guard installed above actually blocks ---")
print("--- a real UDP sendto - proving the guard is live, not just absent from this run ---")
print("=" * 72)
_guard_raised = False
_guard_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    try:
        _guard_probe.sendto(b"probe", ("127.0.0.1", 1812))  # RADIUS's own default port, deliberately
    except NetworkCallBlocked:
        _guard_raised = True
finally:
    _guard_probe.close()
check("[guard] a genuine socket.sendto to 127.0.0.1 is blocked with NetworkCallBlocked, "
      "not silently sent and not left to time out",
      _guard_raised, True)
print("        (the rest of this file running to completion with zero NetworkCallBlocked "
      "failures is itself the complementary proof that nothing below ever needed a real "
      "socket in the first place)")


def add_admin(db, username, **extra):
    row = models.AdminUser(username=username, hashed_password="x", role="admin", **extra)
    db.add(row)
    db.commit()
    db.refresh(row)
    hierarchy.rebuild_path(db, row)
    db.commit()
    return row


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()

node_ok = models.Node(name="node-ok", type=models.NodeType.mikrotik,
                      mt_host="1.1.1.1", mt_username="u", mt_password="p")
node_bad = models.Node(name="node-bad", type=models.NodeType.mikrotik,
                       mt_host="2.2.2.2", mt_username="u", mt_password="p")
db.add_all([node_ok, node_bad])
db.commit()

# This file is a test_*.py, picked up by run_all.py/CI - it must make ZERO
# real network calls, ever. routers/bot.py's _connection_info() calls
# user_ops.get_connection_share() when serializing ANY response that
# includes connections, and that function's OWN wireguard branch makes a
# genuine MikrotikClient network call (to fetch the server's public key for
# the config text) - completely independent of whichever provision_*
# function actually created the row. Found in round 3 via real "Operation
# not permitted"/timeout warnings even though every section below already
# mocks PROVISIONING itself. Patched globally, once, here - never toggled
# per-section - since no finding in this file is about share/config-text
# generation; only the wireguard branch is stubbed, every other protocol
# (openvpn in particular, used by [G1]/[G2]/[H] specifically to avoid
# needing this) still runs its own real, non-networked logic unchanged.
_real_get_connection_share = user_ops.get_connection_share


def _network_free_get_connection_share(connection):
    if connection.type == models.ConnectionType.wireguard:
        node = connection.node
        return {
            "kind": "wireguard", "link": None, "config_text": None,
            "server": node.mt_endpoint_host if node else None,
            "port": node.mt_endpoint_port if node else None,
            "username": None, "password": None, "psk": None,
        }
    return _real_get_connection_share(connection)


user_ops.get_connection_share = _network_free_get_connection_share

principal = BotPrincipal.internal(None)  # unscoped - the shared bot's own principal, today's real default

app = FastAPI()
app.include_router(bot_router.router)
app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[get_bot_principal] = lambda: principal
client = TestClient(app)


# ===========================================================================
print("=" * 72)
print("--- [A] create_user's manual connections loop has no per-connection ---")
print("--- error containment, unlike every sibling path in the codebase   ---")
print("=" * 72)

_real_provision_wireguard = user_ops.provision_wireguard
_calls: list[int] = []


def _fake_provision_wireguard(db_, user, node, purchase_batch=None, package_name=None,
                              max_concurrent_sessions=1, speed_limit_mbps=None):
    _calls.append(node.id)
    if node.id == node_bad.id:
        raise HTTPException(400, "خطای شبیه‌سازی‌شده: میکروتیک در دسترس نیست")
    conn = models.Connection(
        user_id=user.id, node_id=node.id, type=models.ConnectionType.wireguard,
        wg_peer_name=f"user-{user.username}-fake", wg_public_key="pub", wg_private_key="priv",
        wg_client_address="10.0.0.2/32",
    )
    db_.add(conn)
    db_.commit()
    db_.refresh(conn)
    return conn


user_ops.provision_wireguard = _fake_provision_wireguard
try:
    resp = client.post(
        "/api/bot/users",
        json={
            "username": "phantom_customer",
            "connections": [
                {"node_id": node_ok.id, "protocol": "wireguard"},
                {"node_id": node_bad.id, "protocol": "wireguard"},
            ],
        },
    )
finally:
    user_ops.provision_wireguard = _real_provision_wireguard

check("the request as a whole is refused (the customer/bot sees a clean failure)",
      resp.status_code, 400)
check("provision_wireguard really was called for BOTH nodes (order confirmed) before the failure",
      _calls, [node_ok.id, node_bad.id])

phantom_user = db.query(models.User).filter(models.User.username == "phantom_customer").first()
check("FINDING: despite the 400, a real User row was left durably committed",
      phantom_user is not None, True)

if phantom_user:
    phantom_conn = db.query(models.Connection).filter(models.Connection.user_id == phantom_user.id).all()
    check("FINDING: node_ok's Connection is ALSO durably committed, live and unbilled",
          [c.node_id for c in phantom_conn], [node_ok.id])
    check("...and no Purchase was ever created for it",
          db.query(models.Purchase).filter(models.Purchase.user_id == phantom_user.id).count(), 0)
    check("...and the reseller/superadmin was never billed for it either (no wrongful charge)",
          db.query(models.LedgerEntry).count(), 0)

    print("\n--- [A2] the phantom row also makes the failure UNRECOVERABLE by simple retry ---")
    resp2 = client.post("/api/bot/users", json={"username": "phantom_customer", "connections": []})
    check("retrying with the SAME username is refused - \"username already taken\"", resp2.status_code, 400)
    check("...giving no hint this is the customer's own half-created account",
          "قبلا ثبت شده" in (resp2.json().get("detail") or ""), True)


# ===========================================================================
print("\n" + "=" * 72)
print("--- [B] delete_user_cascade: real behavioral reproduction of a ---")
print("--- partial remote-deprovision failure (round 1's own [B] never ---")
print("--- actually drove this - it only inspected sibling functions' ---")
print("--- source for a try/except keyword, which is not this at all) ---")
print("=" * 72)

user_b = models.User(username="del_test_user")
db.add(user_b)
db.commit()
conn1 = models.Connection(user_id=user_b.id, node_id=node_ok.id, type=models.ConnectionType.wireguard,
                          wg_peer_name="peer1", wg_public_key="k1")
conn2 = models.Connection(user_id=user_b.id, node_id=node_ok.id, type=models.ConnectionType.wireguard,
                          wg_peer_name="peer2", wg_public_key="k2")
db.add_all([conn1, conn2])
db.commit()
conn1_id, conn2_id, user_b_id = conn1.id, conn2.id, user_b.id

_real_deprovision = user_ops.deprovision_connection
_remote_removed: list[int] = []


def _fake_deprovision_fails_second(connection):
    _remote_removed.append(connection.id)
    if connection.id == conn2_id:
        raise HTTPException(400, "خطای شبیه‌سازی‌شده: اتصال دوم از سرور حذف نشد")
    # connection 1: the mock represents a REAL remote removal that
    # succeeded - deprovision_connection itself never touches the DB (see
    # its own docstring), so nothing else happens here.


user_ops.deprovision_connection = _fake_deprovision_fails_second
try:
    try:
        user_ops.delete_user_cascade(db, user_b)
        raised = False
    except HTTPException:
        raised = True
finally:
    user_ops.deprovision_connection = _real_deprovision

check("delete_user_cascade raises when the second connection's remote deprovision fails", raised, True)
check("remote deprovision was attempted for BOTH connections, in order, before the failure",
      _remote_removed, [conn1_id, conn2_id])

# Mirrors what a fresh per-request session's own get_db(...)/finally:
# db.close() would already do in production - this test reuses one
# session, same as the rest of this suite's own convention.
db.rollback()

check("FINDING: the User row still exists in the DB (the delete never committed)",
      db.get(models.User, user_b_id) is not None, True)
check("FINDING: connection 1's DB row ALSO still exists, even though its remote peer was "
      "genuinely already removed one line above - DB and remote are now out of sync",
      db.get(models.Connection, conn1_id) is not None, True)
check("connection 2's DB row still exists too (its own deprovision never even completed)",
      db.get(models.Connection, conn2_id) is not None, True)

print("\n--- [B2] retry: idempotent remote removal lets the SAME delete succeed the second time ---")
_remote_removed2: list[int] = []


def _fake_deprovision_retry_ok(connection):
    # Both succeed this time - mirrors the REAL deprovision_connection's
    # own tolerance for an already-missing wireguard peer (it logs a
    # warning and continues rather than raising - see that function's own
    # comment), which is exactly what a genuine retry after [B]'s failure
    # would hit for connection 1.
    _remote_removed2.append(connection.id)


user_ops.deprovision_connection = _fake_deprovision_retry_ok
try:
    user_ops.delete_user_cascade(db, db.get(models.User, user_b_id))
    raised2 = False
except HTTPException:
    raised2 = True
finally:
    user_ops.deprovision_connection = _real_deprovision

check("retrying the delete now that both remote removals succeed completes without error", raised2, False)
check("...and BOTH connections were retried, including connection 1 (an idempotent no-op "
      "on the real router/client, not a duplicate error)",
      _remote_removed2, [conn1_id, conn2_id])
check("User row is finally gone", db.get(models.User, user_b_id), None)
check("both Connection rows are finally gone too",
      [db.get(models.Connection, conn1_id), db.get(models.Connection, conn2_id)], [None, None])


# ===========================================================================
print("\n" + "=" * 72)
print("--- [C] apply_package_as_purchase: partial connection fulfillment ---")
print("--- still charges/delivers in full - bot AND panel paths ---")
print("=" * 72)

admin_c = add_admin(db, "admin_c", balance=1_000_000)
cust_c = models.User(username="cust_c", owner_admin_id=admin_c.id)
pkg_c = models.Package(name="pkg-c", quota_gb=1, duration_days=30, price=10000,
                       enabled=True, bot_enabled=True, owner_admin_id=admin_c.id)
db.add_all([cust_c, pkg_c])
db.commit()
pc_ok = models.PackageConnection(package_id=pkg_c.id, node_id=node_ok.id, protocol=models.ConnectionType.wireguard)
pc_bad = models.PackageConnection(package_id=pkg_c.id, node_id=node_bad.id, protocol=models.ConnectionType.wireguard)
db.add_all([pc_ok, pc_bad])
db.commit()

before_purchases_c = db.query(models.Purchase).count()
before_connections_c = db.query(models.Connection).count()
before_balance_c = admin_c.balance

user_ops.provision_wireguard = _fake_provision_wireguard
try:
    resp = client.post(
        f"/api/bot/users/{cust_c.username}/purchase-package",
        json={"package_id": pkg_c.id, "paid_amount": 10000},
    )
finally:
    user_ops.provision_wireguard = _real_provision_wireguard

check("[bot] a package with one good and one bad bundled connection still reports success overall",
      resp.status_code, 200)
purchase_conns = resp.json().get("connections", []) if resp.status_code == 200 else []
check("[bot] ...and the customer actually only received ONE of the two promised services",
      len(purchase_conns), 1)
check("[bot] exactly one new Purchase row exists for this attempt",
      db.query(models.Purchase).count() - before_purchases_c, 1)
check("[bot] exactly one new Connection row exists (not two)",
      db.query(models.Connection).count() - before_connections_c, 1)
entry = db.query(models.LedgerEntry).filter(models.LedgerEntry.user_id == cust_c.id).first()
check("[bot] FINDING: the CUSTOMER-side ledger row (accounting.record via _record_bot_sale) "
      "shows the FULL package price (10000) for HALF the bundled services",
      entry.amount if entry else None, 10000)
db.refresh(admin_c)
check("[bot] FINDING: separately, the RESELLER's own wallet (admin_c.balance, charged via "
      "_charge_seller/admin_billing.charge_for_renewal - a DIFFERENT ledger row/account than "
      "the customer-sale entry checked just above) was ALSO debited the FULL package price, "
      "not a partial/prorated amount - round 2's own [C] never actually asserted this, only "
      "the customer-facing sale amount",
      before_balance_c - admin_c.balance, 10000)
check("[bot] FINDING: BotPurchaseResponse carries no field naming what was skipped or why - "
      "the caller cannot tell this was a partial fulfillment at all from the response alone",
      "skip" in "".join(resp.json().keys()).lower() if resp.status_code == 200 else None, False)

print("\n--- [C2] the SAME gap on the PANEL's apply_package endpoint (routers/users.py) ---")
print("--- (round 2's own [C2] used a superadmin, who is NEVER charged - so it proved ---")
print("--- nothing about the panel's own wallet-charge behavior; fixed to a real, ---")
print("--- non-superadmin admin with a real balance below) ---")

admin_c2 = add_admin(db, "admin_c2", balance=1_000_000)
cust_c2 = models.User(username="cust_c2", owner_admin_id=admin_c2.id)
pkg_c2 = models.Package(name="pkg-c2", quota_gb=1, duration_days=30, price=10000,
                        enabled=True, bot_enabled=True, owner_admin_id=admin_c2.id)
db.add_all([cust_c2, pkg_c2])
db.commit()
db.add_all([
    models.PackageConnection(package_id=pkg_c2.id, node_id=node_ok.id, protocol=models.ConnectionType.wireguard),
    models.PackageConnection(package_id=pkg_c2.id, node_id=node_bad.id, protocol=models.ConnectionType.wireguard),
])
db.commit()

app_panel = FastAPI()
app_panel.include_router(users_router.router)
app_panel.dependency_overrides[get_db] = lambda: db
app_panel.dependency_overrides[get_current_admin] = lambda: db.get(models.AdminUser, admin_c2.id)
panel_client = TestClient(app_panel)

before_purchases_c2 = db.query(models.Purchase).count()
before_balance_c2 = admin_c2.balance
user_ops.provision_wireguard = _fake_provision_wireguard
try:
    presp = panel_client.post(f"/api/users/{cust_c2.id}/apply-package", json={"package_id": pkg_c2.id})
finally:
    user_ops.provision_wireguard = _real_provision_wireguard

check("[panel] apply_package with one good and one bad connection ALSO reports plain success",
      presp.status_code, 200)
check("[panel] exactly one new Purchase row for this attempt (not zero, not two)",
      db.query(models.Purchase).count() - before_purchases_c2, 1)
panel_purchase = (
    db.query(models.Purchase).filter(models.Purchase.user_id == cust_c2.id).order_by(models.Purchase.id.desc()).first()
)
check("[panel] ...with only ONE of the two bundled connections actually delivered",
      len(panel_purchase.connections) if panel_purchase else None, 1)
db.refresh(admin_c2)
check("[panel] FINDING: admin_c2 (a REAL non-superadmin admin, not a superadmin like round 2's "
      "own [C2]) was charged the FULL package price (10000) via charge_for_package at "
      "routers/users.py:1005, for a purchase that only delivered HALF the bundled connections - "
      "this is the actual financial assertion round 2's [C2] was missing, not just \"a Purchase "
      "row exists\"",
      before_balance_c2 - admin_c2.balance, 10000)
panel_ledger = (
    db.query(models.LedgerEntry)
    .filter(models.LedgerEntry.admin_id == admin_c2.id, models.LedgerEntry.kind == "admin_credit_spend")
    .first()
)
check("[panel] ...backed by a real admin_credit_spend Ledger row for the full amount",
      panel_ledger.amount if panel_ledger else None, 10000)


# ===========================================================================
print("\n" + "=" * 72)
print("--- [D] remote resource created BEFORE the Connection row's own commit -")
print("--- a failure in THAT commit leaves an orphan on the real infrastructure -")
print("--- reproduced independently for all three provisioning backends ---")
print("=" * 72)


class _FakeMikrotikCM:
    def __init__(self):
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ensure_wireguard_interface(self, *a, **k):
        self.calls.append("ensure_wireguard_interface")

    def ensure_interface_address(self, *a, **k):
        self.calls.append("ensure_interface_address")

    def set_interface_address(self, *a, **k):
        self.calls.append("set_interface_address")

    def add_peer(self, *a, **k):
        self.calls.append("add_peer")

    def upsert_simple_queue(self, *a, **k):
        self.calls.append("upsert_simple_queue")


class _FailingCommitSession:
    """Wraps the real db session so exactly the NEXT commit() call raises,
    then every commit after that behaves normally again - isolates the
    failure to precisely the Connection row's own commit inside
    provision_wireguard/provision_xray/provision_softether, without
    needing a genuine DB-level fault to trigger it."""

    def __init__(self, real_db):
        self._real_db = real_db
        self.armed = False

    def commit(self):
        if self.armed:
            self.armed = False
            raise RuntimeError("simulated DB failure exactly when the Connection row's own commit runs")
        return self._real_db.commit()

    def __getattr__(self, name):
        return getattr(self._real_db, name)


print("\n--- [D1] WireGuard (provision_wireguard, user_ops.py) ---")

user_d1 = models.User(username="del_d1_user")
db.add(user_d1)
db.commit()

fake_mt = _FakeMikrotikCM()
real_for_node = user_ops.MikrotikClient.for_node
user_ops.MikrotikClient.for_node = classmethod(lambda cls, node: fake_mt)
wrapped_db = _FailingCommitSession(db)
wrapped_db.armed = True
try:
    try:
        user_ops.provision_wireguard(wrapped_db, user_d1, node_ok)
        raised_d1 = False
    except RuntimeError:
        raised_d1 = True
finally:
    user_ops.MikrotikClient.for_node = real_for_node

check("[D1] the simulated commit failure really did propagate out of provision_wireguard", raised_d1, True)
check("[D1] FINDING: the remote MikroTik calls (interface + add_peer) already ran BEFORE that "
      "commit - add_peer was really called, so a real peer now exists on the router",
      "add_peer" in fake_mt.calls, True)
db.rollback()
check("[D1] FINDING: ...but no Connection row exists for it at all - the peer is a pure orphan, "
      "nothing in the DB references it, and nothing will ever clean it up automatically",
      db.query(models.Connection).filter(models.Connection.user_id == user_d1.id).count(), 0)

print("\n--- [D2] Xray (provision_xray, user_ops.py) ---")

node_xray = models.Node(name="node-xray", type=models.NodeType.xray, xr_ssh_host="4.4.4.4",
                        xr_ssh_username="root", xr_inbound_tag="inbound-1")
db.add(node_xray)
db.commit()
user_d2 = models.User(username="del_d2_user")
db.add(user_d2)
db.commit()


class _FakeXrayCM:
    def __init__(self):
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add_client(self, inbound_tag, email, flow=""):
        self.calls.append("add_client")
        return "fake-uuid-1234"


fake_xc = _FakeXrayCM()
real_client_for_node = user_ops.client_for_node
user_ops.client_for_node = lambda node: fake_xc
wrapped_db2 = _FailingCommitSession(db)
wrapped_db2.armed = True
try:
    try:
        user_ops.provision_xray(wrapped_db2, user_d2, node_xray)
        raised_d2 = False
    except RuntimeError:
        raised_d2 = True
finally:
    user_ops.client_for_node = real_client_for_node

check("[D2] the simulated commit failure propagated out of provision_xray", raised_d2, True)
check("[D2] FINDING: add_client already ran on the (mocked) Xray node before that commit",
      "add_client" in fake_xc.calls, True)
db.rollback()
check("[D2] FINDING: ...but no Connection row exists for it - the Xray client is a pure orphan too",
      db.query(models.Connection).filter(models.Connection.user_id == user_d2.id).count(), 0)

print("\n--- [D3] SoftEther (provision_softether, user_ops.py) ---")

node_se = models.Node(name="node-se", type=models.NodeType.softether, se_host="5.5.5.5",
                      se_hub_name="hub1", se_admin_password="p")
db.add(node_se)
db.commit()
user_d3 = models.User(username="del_d3_user")
db.add(user_d3)
db.commit()


class _FakeSoftEtherCM:
    def __init__(self):
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add_client(self, username, password):
        self.calls.append("add_client")


fake_sc = _FakeSoftEtherCM()
real_se_client_for_node = user_ops.softether_client_for_node
user_ops.softether_client_for_node = lambda node: fake_sc
wrapped_db3 = _FailingCommitSession(db)
wrapped_db3.armed = True
try:
    try:
        user_ops.provision_softether(wrapped_db3, user_d3, node_se)
        raised_d3 = False
    except RuntimeError:
        raised_d3 = True
finally:
    user_ops.softether_client_for_node = real_se_client_for_node

check("[D3] the simulated commit failure propagated out of provision_softether", raised_d3, True)
check("[D3] FINDING: add_client already ran on the (mocked) SoftEther hub before that commit",
      "add_client" in fake_sc.calls, True)
db.rollback()
check("[D3] FINDING: ...but no Connection row exists for it either - same orphan pattern, "
      "all three provisioning backends share it because all three call the remote side "
      "before their own db.commit(), not because of a bug specific to any one of them",
      db.query(models.Connection).filter(models.Connection.user_id == user_d3.id).count(), 0)


# ===========================================================================
print("\n" + "=" * 72)
print("--- [E] panel create_user: wallet charge commits BEFORE the User row's ---")
print("--- own commit - a failure there leaves the admin charged with nothing ---")
print("=" * 72)

admin_e = add_admin(db, "admin_e", balance=1_000_000)
pkg_e = models.Package(name="pkg-e", quota_gb=1, duration_days=30, price=50000,
                       cooperation_price=20000, enabled=True, bot_enabled=True,
                       owner_admin_id=admin_e.id)
db.add(pkg_e)
db.commit()

app_e = FastAPI()
app_e.include_router(users_router.router)
app_e.dependency_overrides[get_db] = lambda: db
app_e.dependency_overrides[get_current_admin] = lambda: admin_e
client_e = TestClient(app_e)

before_balance_e = admin_e.balance
before_ledger_e = db.query(models.LedgerEntry).count()

_real_commit_direct = db.commit
_commit_calls: list[int] = []


def _commit_fails_on_second_call():
    _commit_calls.append(1)
    if len(_commit_calls) == 2:
        raise RuntimeError("simulated DB failure exactly when the new User row's own commit runs")
    return _real_commit_direct()


db.commit = _commit_fails_on_second_call
try:
    try:
        resp_e = client_e.post("/api/users", json={"username": "finding_e_user", "package_id": pkg_e.id})
        raised_e = False
    except Exception:
        raised_e = True
finally:
    db.commit = _real_commit_direct

check("[E] the simulated failure on the User row's own commit really propagated "
      "(TestClient surfaces the unhandled server error as an exception, not a clean response)",
      raised_e, True)
db.rollback()
db.refresh(admin_e)
check("[E] FINDING: the admin's wallet was ALREADY durably debited (charge_for_package's own "
      "internal commit succeeded first, and nothing rolled that back)",
      before_balance_e - admin_e.balance, 20000)
check("[E] FINDING: a matching Ledger row was durably written too",
      db.query(models.LedgerEntry).count() - before_ledger_e, 1)
check("[E] FINDING: ...but no User row was ever created - the refund-on-failure try/except in "
      "routers/users.py's create_user only wraps provision_package_connections, which starts "
      "AFTER this exact commit, so it never got a chance to run",
      db.query(models.User).filter(models.User.username == "finding_e_user").first(), None)

print("\n--- [E2] CORRECTED (round 2's own [E2] conclusion was wrong): the bot's REVERSED ---")
print("--- ordering (charge AFTER create) is not safe either - see [G1]/[G2] below ---")
# Round 2's own [E2] checked only that _charge_seller runs textually AFTER
# create_user_record and concluded bot signups "do NOT have finding E's
# exact charge-before-create ordering" - true as far as it went, but it
# never checked what happens when that LATER charge fails. It does not
# roll back what already committed - see [G1]/[G2].

import inspect  # noqa: E402

bulk_src = inspect.getsource(users_router.bulk_create_users)
check("panel bulk-create's own try/except wraps the ENTIRE user_ops.bulk_create_users call "
      "(not just provisioning) and DOES call refund_for_package on any exception",
      "except Exception" in bulk_src and "refund_for_package" in bulk_src, True)
print("        NOTE (not an assertion - a source-level string match can only prove a refund "
      "CALL exists, not what it accomplishes): this says nothing about whether users/"
      "connections already committed BEFORE the failure get cleaned up too - see [H] below, "
      "which is round 2's own correction: a refund is not the same thing as a rollback.")


# ===========================================================================
print("\n" + "=" * 72)
print("--- [G1] bot create_user: reseller charge failure AFTER create+provision+absorb ---")
print("--- still leaves a fully committed, FREE User+Connection+Purchase behind ---")
print("=" * 72)
# OpenVPN/L2TP connections need no remote call at all (RADIUS-based - see
# _provision_ppp's own docstring), so node_ok (already a plain mikrotik
# node) is used here completely unmocked - this reproduction needs no
# client mocking of any kind.

reseller_g1 = add_admin(db, "reseller_g1", balance=0)
pkg_g1 = models.Package(name="pkg-g1", quota_gb=1, duration_days=30, price=20000,
                        cooperation_price=20000, enabled=True, bot_enabled=True,
                        owner_admin_id=reseller_g1.id)
db.add(pkg_g1)
db.commit()
# A bundled connection too (openvpn - no remote call needed), so [G2] below
# (purchase_package, which provisions package.connections, not a manual
# payload.connections list) has something real to provision.
db.add(models.PackageConnection(package_id=pkg_g1.id, node_id=node_ok.id, protocol=models.ConnectionType.openvpn))
db.commit()

resp_g1 = client.post(
    "/api/bot/users",
    json={
        "username": "g1_customer",
        "owner_admin_id": reseller_g1.id,
        "package_id": pkg_g1.id,
        "connections": [{"node_id": node_ok.id, "protocol": "openvpn"}],
        "package_name": "pkg-g1",
    },
)

check("[G1] the request is refused - the reseller cannot afford this customer's package",
      resp_g1.status_code, 400)
g1_user = db.query(models.User).filter(models.User.username == "g1_customer").first()
check("[G1] FINDING: despite the refusal, a real User row was left durably committed",
      g1_user is not None, True)
if g1_user:
    check("[G1] FINDING: a real Connection (OpenVPN credential) was ALSO committed",
          db.query(models.Connection).filter(models.Connection.user_id == g1_user.id).count(), 1)
    check("[G1] FINDING: absorb_legacy_pool_into_purchase's own pending Purchase (added+flushed, "
          "never committed by that function itself) was made durable ANYWAY - purely as a side "
          "effect of debit_admin's own db.commit() on ITS failure path (see admin_billing.py's "
          "debit_admin: db.commit() runs even when rowcount==0, before raising)",
          db.query(models.Purchase).filter(models.Purchase.user_id == g1_user.id).count(), 1)
db.refresh(reseller_g1)
check("[G1] ...and the reseller's balance genuinely was NOT charged (the debit's own "
      "conditional UPDATE affected 0 rows) - so this customer received a fully free, "
      "fully provisioned, fully independent-Purchase service",
      reseller_g1.balance, 0)


print("\n" + "=" * 72)
print("--- [G2] bot purchase_package: same reseller-charge-failure pattern, existing customer ---")
print("=" * 72)

cust_g2 = models.User(username="g2_customer", owner_admin_id=reseller_g1.id)
db.add(cust_g2)
db.commit()

resp_g2 = client.post(
    f"/api/bot/users/{cust_g2.username}/purchase-package",
    json={"package_id": pkg_g1.id},
)
check("[G2] the request is refused for the same insufficient-balance reason", resp_g2.status_code, 400)
check("[G2] FINDING: apply_package_as_purchase's OWN internal db.commit() (at the end of that "
      "function, unconditional - see user_ops.py) already made the Purchase durable BEFORE "
      "_charge_seller even runs, so this is actually a MORE direct version of [G1]'s gap, not "
      "dependent on debit_admin's failure-path commit at all",
      db.query(models.Purchase).filter(models.Purchase.user_id == cust_g2.id).count(), 1)
check("[G2] ...with a real committed Connection too",
      db.query(models.Connection).filter(models.Connection.user_id == cust_g2.id).count(), 1)
db.refresh(reseller_g1)
check("[G2] ...and, again, genuinely no charge went through", reseller_g1.balance, 0)


# ===========================================================================
print("\n" + "=" * 72)
print("--- [H] bulk_create_users: a full refund does NOT undo users already committed ---")
print("--- BEFORE the batch failed partway through ---")
print("=" * 72)

reseller_h = add_admin(db, "reseller_h", balance=100000)
pkg_h = models.Package(name="pkg-h", quota_gb=1, duration_days=30, price=50000,
                       cooperation_price=50000, enabled=True, bot_enabled=True,
                       owner_admin_id=reseller_h.id)
db.add(pkg_h)
db.commit()
# openvpn - no remote call needed, so this section needs no client mock at
# all (the real provision_wireguard would otherwise attempt a genuine,
# slow, failing network call to node_ok's fake mt_host).
db.add(models.PackageConnection(package_id=pkg_h.id, node_id=node_ok.id, protocol=models.ConnectionType.openvpn))
db.commit()

app_h = FastAPI()
app_h.include_router(users_router.router)
app_h.dependency_overrides[get_db] = lambda: db
app_h.dependency_overrides[get_current_admin] = lambda: db.get(models.AdminUser, reseller_h.id)
client_h = TestClient(app_h)

before_balance_h = reseller_h.balance

_real_provision_package_connections = user_ops.provision_package_connections
_ppc_calls: list[str] = []


def _fake_provision_package_connections(db_, user, package):
    _ppc_calls.append(user.username)
    if len(_ppc_calls) == 2:
        # Not an HTTPException (which provision_package_connections' own
        # per-connection try/except would swallow) - a genuine crash class,
        # matching the repro shape ("provisioning کاربر دوم RuntimeError داد").
        raise RuntimeError("simulated hard crash provisioning the SECOND bulk-created user")
    return _real_provision_package_connections(db_, user, package)


user_ops.provision_package_connections = _fake_provision_package_connections
try:
    try:
        resp_h = client_h.post(
            "/api/users/bulk",
            json={"prefix": "bulkh", "count": 2, "package_id": pkg_h.id},
        )
        raised_h = False
    except RuntimeError:
        raised_h = True
finally:
    user_ops.provision_package_connections = _real_provision_package_connections

check("[H] the batch as a whole is refused (the crash on user #2 propagates)", raised_h, True)
db.rollback()
db.refresh(reseller_h)
check("[H] the refund-on-exception path DID run and gave back the FULL reserved amount "
      "(2 units x 50000 = 100000) - confirming round 2's [H] premise that refund itself works",
      reseller_h.balance, before_balance_h)

bulkh_users = db.query(models.User).filter(models.User.username.like("bulkh%")).order_by(models.User.username).all()
check("[H] FINDING (round 3 correction - round 2's own [H] only checked bulkh1): exactly "
      "TWO bulkh users exist, not one - the batch left BOTH a fully-provisioned phantom "
      "AND a half-built phantom behind, not just the former",
      [u.username for u in bulkh_users], ["bulkh1", "bulkh2"])

bulkh1 = next((u for u in bulkh_users if u.username == "bulkh1"), None)
check("[H] bulkh1 (provisioned successfully BEFORE the crash on user #2): one real "
      "committed Connection",
      db.query(models.Connection).filter(models.Connection.user_id == bulkh1.id).count() if bulkh1 else None, 1)
check("[H] bulkh1: one real committed Purchase (absorb_legacy_pool_into_purchase's own "
      "commit, called right after provision_package_connections succeeds for user #1)",
      db.query(models.Purchase).filter(models.Purchase.user_id == bulkh1.id).count() if bulkh1 else None, 1)

bulkh2 = next((u for u in bulkh_users if u.username == "bulkh2"), None)
check("[H] FINDING: bulkh2 (the one that CRASHED mid-provisioning) is ALSO durably "
      "committed - create_user_record + its package_id/quota/expire_at fields are set and "
      "db.commit()'d BEFORE provision_package_connections is ever called, so the crash "
      "inside that call does not undo this earlier commit",
      bulkh2 is not None, True)
if bulkh2:
    check("[H] bulkh2: package_id is set to pkg_h, exactly like a real customer of it",
          bulkh2.package_id, pkg_h.id)
    check("[H] bulkh2: quota/expiry were also stamped from pkg_h before the crash",
          (bulkh2.total_quota_bytes, bulkh2.expire_at is not None), (pkg_h.quota_gb * 1024 ** 3, True))
    check("[H] FINDING: ...but bulkh2 has ZERO Connections (the crash happened INSIDE "
          "provision_package_connections, before it created anything for this user)",
          db.query(models.Connection).filter(models.Connection.user_id == bulkh2.id).count(), 0)
    check("[H] FINDING: ...and ZERO Purchases too (absorb_legacy_pool_into_purchase is only "
          "called when result['created'] is non-empty, which never happened for bulkh2) - "
          "a genuinely half-built, unusable phantom account, a DIFFERENT shape of orphan "
          "than bulkh1's, and any cleanup design has to handle both",
          db.query(models.Purchase).filter(models.Purchase.user_id == bulkh2.id).count(), 0)

spend_entries = db.query(models.LedgerEntry).filter(
    models.LedgerEntry.admin_id == reseller_h.id, models.LedgerEntry.kind == "admin_credit_spend",
).all()
refund_entries = db.query(models.LedgerEntry).filter(
    models.LedgerEntry.admin_id == reseller_h.id, models.LedgerEntry.kind == "admin_credit_refund",
).all()
check("[H] exactly one admin_credit_spend Ledger row, for the full reserved batch amount "
      "(2 units x 50000 = 100000) - not just \"count >= 2\"",
      [e.amount for e in spend_entries], [100000])
check("[H] exactly one admin_credit_refund Ledger row, for the SAME full amount",
      [e.amount for e in refund_entries], [100000])
# A real composite invariant, not a vacuous True==True placeholder (round
# 3 review caught the previous version of this line never actually
# checked anything) - every value below is independently re-derived from
# the DB, mirroring exactly what "the reseller paid nothing net, and both
# phantoms survive" has to mean together.
net_result_h = (
    reseller_h.balance == before_balance_h,
    sum(e.amount for e in spend_entries) == 100000,
    sum(e.amount for e in refund_entries) == 100000,
    len(spend_entries) == 1,
    len(refund_entries) == 1,
    bulkh1 is not None,
    bulkh2 is not None,
)
check("[H] net result (composite invariant): balance fully restored, spend and refund each "
      "exactly one row of exactly 100000, and BOTH phantoms (bulkh1 fully provisioned, "
      "bulkh2 half-built) still exist",
      net_result_h, (True, True, True, True, True, True, True))


# ===========================================================================
print("\n" + "=" * 72)
print("--- [F] CHARACTERIZATION, not a confirmed finding (round 2's own correction: ---")
print("--- for an ORDINARY package, buying it twice may be legitimate product ---")
print("--- behavior - Purchase exists precisely so a customer can hold several ---")
print("--- independent services. Two sequential 200s prove duplicate purchases ---")
print("--- are POSSIBLE, not that they are unintended (no idempotency-key exists ---")
print("--- to tell double-click/retry intent apart from a deliberate second buy) ---")
print("=" * 72)

admin_f = add_admin(db, "admin_f", balance=1_000_000)
cust_f = models.User(username="cust_f", owner_admin_id=admin_f.id)
pkg_f = models.Package(name="pkg-f", quota_gb=1, duration_days=30, price=5000,
                       enabled=True, bot_enabled=True, owner_admin_id=admin_f.id)
db.add_all([cust_f, pkg_f])
db.commit()
db.add(models.PackageConnection(package_id=pkg_f.id, node_id=node_ok.id, protocol=models.ConnectionType.wireguard))
db.commit()

user_ops.provision_wireguard = _fake_provision_wireguard
try:
    resp_f1 = client.post(f"/api/bot/users/{cust_f.username}/purchase-package",
                          json={"package_id": pkg_f.id, "paid_amount": 5000})
    resp_f2 = client.post(f"/api/bot/users/{cust_f.username}/purchase-package",
                          json={"package_id": pkg_f.id, "paid_amount": 5000})
finally:
    user_ops.provision_wireguard = _real_provision_wireguard

check("[F] characterization: an identical purchase-package request, sent twice with no "
      "idempotency key anywhere in the payload/schema, succeeds BOTH times - this alone does "
      "NOT prove a bug (a customer may legitimately buy the same ordinary package twice)",
      (resp_f1.status_code, resp_f2.status_code), (200, 200))
check("[F] ...creating TWO separate Purchase rows - by design, since Purchase exists so a "
      "customer can hold several independent services at once",
      db.query(models.Purchase).filter(models.Purchase.user_id == cust_f.id).count(), 2)
check("[F] ...and charging twice, matching two purchases (not evidence of a billing bug by "
      "itself - see [F-race] below for the ACTUAL concurrency question: one_time_per_user)",
      db.query(models.LedgerEntry).filter(models.LedgerEntry.user_id == cust_f.id).count(), 2)

print("\n--- [F2] the SAME characterization on the panel's apply-package ---")
# Reuses admin_f (not a fresh superadmin) - _get_scoped_package now enforces
# full package isolation even for a superadmin (see its own docstring:
# "a superadmin creating a user directly can only pick their own 'global'
# packages"), so the acting admin here must actually OWN pkg_f.
cust_f2 = models.User(username="cust_f2", owner_admin_id=admin_f.id)
db.add(cust_f2)
db.commit()

app_f = FastAPI()
app_f.include_router(users_router.router)
app_f.dependency_overrides[get_db] = lambda: db
app_f.dependency_overrides[get_current_admin] = lambda: admin_f
panel_client_f = TestClient(app_f)

user_ops.provision_wireguard = _fake_provision_wireguard
try:
    presp_f1 = panel_client_f.post(f"/api/users/{cust_f2.id}/apply-package", json={"package_id": pkg_f.id})
    presp_f2 = panel_client_f.post(f"/api/users/{cust_f2.id}/apply-package", json={"package_id": pkg_f.id})
finally:
    user_ops.provision_wireguard = _real_provision_wireguard

check("[F2] characterization: the panel's apply-package has the SAME shape - two identical "
      "requests, no dedup key, both succeed",
      (presp_f1.status_code, presp_f2.status_code), (200, 200))
check("[F2] ...creating two Purchase rows for the same user from the same package - same "
      "by-design caveat as [F]",
      db.query(models.Purchase).filter(models.Purchase.user_id == cust_f2.id).count(), 2)


# ===========================================================================
print("\n" + "=" * 72)
print("--- [F-race] the ACTUAL concurrency question: one_time_per_user, two real ---")
print("--- sessions, a controlled barrier forcing both past the check before ---")
print("--- either commits - the invariant this feature promises is 'only once' ---")
print("=" * 72)
# Two independent sqlite CONNECTIONS on the same on-disk file (not
# StaticPool/memory, which would share one connection and prove nothing
# about real concurrency - same reasoning as test_phase_c_lifecycle.py's
# own two-session lock test). _ensure_one_time_package_not_reused is
# wrapped so that, AFTER the real (unlocked) check has already found no
# existing Purchase for either thread, both threads block on a 2-party
# barrier - guaranteeing BOTH pass the check before EITHER proceeds toward
# its own commit, rather than hoping OS thread scheduling happens to
# interleave them unluckily. This makes the race deterministic instead of
# flaky.

# A single TemporaryDirectory, shared by [F-race] and [F-race2] below (the
# latter deliberately reuses this same on-disk file - see its own comment),
# cleaned up in a finally block that runs on success OR failure, closing
# every session and disposing every engine opened against it first. Round
# 3 review found 21 leaked lifecycle_race_* directories under
# tempfile.gettempdir() from earlier manual runs - mkdtemp() alone never
# cleans up; TemporaryDirectory's own __exit__ (here, the finally block's
# explicit cleanup()) does.
_race_tmp = tempfile.TemporaryDirectory(prefix="lifecycle_race_")
_race_sessions: list = []
_race_engines: list = []
try:
    tmpdir_race = _race_tmp.name
    race_db_path = os.path.join(tmpdir_race, "onetime.db")
    race_file_url = f"sqlite:///{race_db_path}"

    engine_r1 = create_engine(race_file_url, connect_args={"check_same_thread": False, "timeout": 10})
    _race_engines.append(engine_r1)
    models.Base.metadata.create_all(engine_r1)
    db_r1 = sessionmaker(bind=engine_r1)()
    _race_sessions.append(db_r1)

    engine_r2 = create_engine(race_file_url, connect_args={"check_same_thread": False, "timeout": 10})
    _race_engines.append(engine_r2)
    db_r2 = sessionmaker(bind=engine_r2)()
    _race_sessions.append(db_r2)

    admin_race = models.AdminUser(username="admin_race_f", hashed_password="x", role="admin", balance=1_000_000)
    db_r1.add(admin_race)
    db_r1.commit()
    db_r1.refresh(admin_race)
    hierarchy.rebuild_path(db_r1, admin_race)
    db_r1.commit()
    admin_race_id = admin_race.id

    node_race_f = models.Node(name="node-race-f", type=models.NodeType.mikrotik,
                              mt_host="6.6.6.6", mt_username="u", mt_password="p")
    db_r1.add(node_race_f)
    db_r1.commit()
    node_race_f_id = node_race_f.id

    pkg_race = models.Package(name="pkg-race-onetime", quota_gb=1, duration_days=30, price=1000,
                              enabled=True, bot_enabled=True, one_time_per_user=True,
                              owner_admin_id=admin_race_id)
    db_r1.add(pkg_race)
    db_r1.commit()
    pkg_race_id = pkg_race.id
    db_r1.add(models.PackageConnection(package_id=pkg_race_id, node_id=node_race_f_id,
                                       protocol=models.ConnectionType.openvpn))
    db_r1.commit()

    cust_race = models.User(username="cust_race_onetime", owner_admin_id=admin_race_id, telegram_id=555000)
    db_r1.add(cust_race)
    db_r1.commit()
    cust_race_id = cust_race.id

    app_r1 = FastAPI()
    app_r1.include_router(bot_router.router)
    app_r1.dependency_overrides[get_db] = lambda: db_r1
    app_r1.dependency_overrides[get_bot_principal] = lambda: BotPrincipal.internal(None)
    client_r1 = TestClient(app_r1)

    app_r2 = FastAPI()
    app_r2.include_router(bot_router.router)
    app_r2.dependency_overrides[get_db] = lambda: db_r2
    app_r2.dependency_overrides[get_bot_principal] = lambda: BotPrincipal.internal(None)
    client_r2 = TestClient(app_r2)

    _barrier = threading.Barrier(2, timeout=10)
    _real_ensure_one_time = bot_router._ensure_one_time_package_not_reused

    def _barrier_ensure_one_time(db_, package, **kwargs):
        _real_ensure_one_time(db_, package, **kwargs)  # must pass (no existing Purchase yet) for both
        _barrier.wait()  # release both only once BOTH have passed the check

    _race_results: dict[str, int] = {}

    def _run_race(name, client_):
        try:
            resp = client_.post(
                f"/api/bot/users/{cust_race.username}/purchase-package",
                json={"package_id": pkg_race_id, "paid_amount": 1000},
            )
            _race_results[name] = resp.status_code
        except threading.BrokenBarrierError:
            _race_results[name] = -1

    # The monkeypatch is applied and restored around the SAME try/finally -
    # if thread start/join or the request itself raises unexpectedly, the
    # patch is still removed before anything later in this file runs with
    # it still installed (round 3 review's own hardening suggestion).
    bot_router._ensure_one_time_package_not_reused = _barrier_ensure_one_time
    try:
        t_r1 = threading.Thread(target=_run_race, args=("r1", client_r1))
        t_r2 = threading.Thread(target=_run_race, args=("r2", client_r2))
        t_r1.start()
        t_r2.start()
        t_r1.join(timeout=15)
        t_r2.join(timeout=15)
    finally:
        bot_router._ensure_one_time_package_not_reused = _real_ensure_one_time

    status_r1 = _race_results.get("r1")
    status_r2 = _race_results.get("r2")
    check("[F-race] both racing requests actually completed (barrier didn't time out/deadlock)",
          None not in (status_r1, status_r2) and -1 not in (status_r1, status_r2), True)

    # Re-open a fresh connection to read the truly-committed final state, same
    # reasoning as test_phase_c_lifecycle.py's own post-race verification -
    # never trust db_r1/db_r2's own possibly-stale in-memory view.
    engine_check_race = create_engine(race_file_url, connect_args={"check_same_thread": False, "timeout": 10})
    _race_engines.append(engine_check_race)
    db_check_race = sessionmaker(bind=engine_check_race)()
    _race_sessions.append(db_check_race)
    purchase_count_race = (
        db_check_race.query(models.Purchase).filter(models.Purchase.user_id == cust_race_id).count()
    )
    ledger_count_race = (
        db_check_race.query(models.LedgerEntry).filter(models.LedgerEntry.user_id == cust_race_id).count()
    )

    print(f"        (observed: status_r1={status_r1}, status_r2={status_r2}, "
          f"committed Purchases for this one_time_per_user package={purchase_count_race}, "
          f"committed Ledger rows={ledger_count_race})")

    check("[F-race] FINDING: with BOTH requests forced past the one-time check before either "
          "commits, more than one is able to complete successfully - there is no unique "
          "constraint or row lock making the SECOND writer's commit fail or wait for the first "
          "one's Purchase to become visible to its own check",
          (status_r1, status_r2).count(200) >= 2, True)
    check("[F-race] FINDING: the 'only once' invariant one_time_per_user promises is violated - "
          "more than one committed Purchase exists for the same user+package",
          purchase_count_race >= 2, True)
    check("[F-race] ...and the reseller/customer was charged more than once for a package "
          "explicitly marked one-time-only",
          ledger_count_race >= 2, True)
    print("        SCOPE NOTE: this race is reproduced on SQLite only. The codebase also "
          "officially supports MySQL/MariaDB, and no MariaDB-backed reproduction has been run - "
          "see the module docstring's own explicit scope limit on this.")

    # =======================================================================
    print("\n" + "=" * 72)
    print("--- [F-race2] the SAME invariant, for the ACTUAL enforcement boundary the code ---")
    print("--- claims: one_time_per_user is checked across every User sharing ONE ---")
    print("--- telegram_id, not just per-user_id - two DIFFERENT usernames, ONE telegram_id ---")
    print("=" * 72)
    # _ensure_one_time_package_not_reused's own telegram_id branch (see
    # routers/bot.py) collects every User.id sharing the claimed telegram_id
    # and checks for an existing Purchase of this package across ALL of them -
    # a fix that only locks on (user_id, package_id) would leave THIS path,
    # the one the feature's own cross-account design is actually FOR, wide
    # open. Reuses admin_race/pkg_race/node_race_f from [F-race] (same file-
    # backed db, same TemporaryDirectory), with two brand-new usernames
    # sharing one telegram_id, each racing POST /api/bot/users (create_user,
    # not purchase_package - the telegram_id-wide branch is create_user's
    # own signup path).

    engine_r3 = create_engine(race_file_url, connect_args={"check_same_thread": False, "timeout": 10})
    _race_engines.append(engine_r3)
    db_r3 = sessionmaker(bind=engine_r3)()
    _race_sessions.append(db_r3)
    engine_r4 = create_engine(race_file_url, connect_args={"check_same_thread": False, "timeout": 10})
    _race_engines.append(engine_r4)
    db_r4 = sessionmaker(bind=engine_r4)()
    _race_sessions.append(db_r4)

    app_r3 = FastAPI()
    app_r3.include_router(bot_router.router)
    app_r3.dependency_overrides[get_db] = lambda: db_r3
    app_r3.dependency_overrides[get_bot_principal] = lambda: BotPrincipal.internal(None)
    client_r3 = TestClient(app_r3)

    app_r4 = FastAPI()
    app_r4.include_router(bot_router.router)
    app_r4.dependency_overrides[get_db] = lambda: db_r4
    app_r4.dependency_overrides[get_bot_principal] = lambda: BotPrincipal.internal(None)
    client_r4 = TestClient(app_r4)

    SHARED_TELEGRAM_ID = 555001

    _barrier2 = threading.Barrier(2, timeout=10)
    _real_ensure_one_time2 = bot_router._ensure_one_time_package_not_reused

    def _barrier_ensure_one_time2(db_, package, **kwargs):
        _real_ensure_one_time2(db_, package, **kwargs)
        _barrier2.wait()

    _race2_results: dict[str, int] = {}

    def _run_race2(name, client_, username):
        try:
            resp = client_.post(
                "/api/bot/users",
                json={
                    "username": username,
                    "owner_admin_id": admin_race_id,
                    "telegram_id": SHARED_TELEGRAM_ID,
                    "package_id": pkg_race_id,
                    "connections": [{"node_id": node_race_f_id, "protocol": "openvpn"}],
                },
            )
            _race2_results[name] = resp.status_code
        except threading.BrokenBarrierError:
            _race2_results[name] = -1

    bot_router._ensure_one_time_package_not_reused = _barrier_ensure_one_time2
    try:
        t_r3 = threading.Thread(target=_run_race2, args=("r3", client_r3, "cust_race2_a"))
        t_r4 = threading.Thread(target=_run_race2, args=("r4", client_r4, "cust_race2_b"))
        t_r3.start()
        t_r4.start()
        t_r3.join(timeout=15)
        t_r4.join(timeout=15)
    finally:
        bot_router._ensure_one_time_package_not_reused = _real_ensure_one_time2

    status_r3 = _race2_results.get("r3")
    status_r4 = _race2_results.get("r4")
    check("[F-race2] both racing signups completed (barrier didn't time out/deadlock)",
          None not in (status_r3, status_r4) and -1 not in (status_r3, status_r4), True)

    engine_check_race2 = create_engine(race_file_url, connect_args={"check_same_thread": False, "timeout": 10})
    _race_engines.append(engine_check_race2)
    db_check_race2 = sessionmaker(bind=engine_check_race2)()
    _race_sessions.append(db_check_race2)
    race2_users = (
        db_check_race2.query(models.User)
        .filter(models.User.telegram_id == SHARED_TELEGRAM_ID)
        .all()
    )
    race2_user_ids = [u.id for u in race2_users]
    race2_purchases = (
        db_check_race2.query(models.Purchase)
        .filter(models.Purchase.package_id == pkg_race_id, models.Purchase.user_id.in_(race2_user_ids))
        .count() if race2_user_ids else 0
    )
    race2_ledger = (
        db_check_race2.query(models.LedgerEntry)
        .filter(models.LedgerEntry.user_id.in_(race2_user_ids))
        .count() if race2_user_ids else 0
    )

    print(f"        (observed: status_r3={status_r3}, status_r4={status_r4}, "
          f"distinct Users sharing telegram_id={SHARED_TELEGRAM_ID}: {len(race2_users)}, "
          f"committed Purchases of this one-time package across them: {race2_purchases}, "
          f"committed Ledger rows: {race2_ledger})")

    check("[F-race2] FINDING: both signups succeeded (200/200) - the telegram_id-wide check "
          "does not prevent two DIFFERENT usernames, sharing one telegram_id, from each buying "
          "the same one-time-only package",
          (status_r3, status_r4).count(200) >= 2, True)
    check("[F-race2] FINDING: two DISTINCT User rows were created for the one shared telegram_id "
          "(exactly the shape the telegram_id-wide check exists to catch AFTER the fact, but "
          "cannot catch DURING a race)",
          len(race2_users), 2)
    check("[F-race2] FINDING: more than one committed Purchase of the SAME one-time package "
          "exists across those two accounts - the cross-account invariant is violated, not just "
          "the single-account one [F-race] already proved",
          race2_purchases >= 2, True)
    check("[F-race2] ...and charged/ledgered more than once for it too",
          race2_ledger >= 2, True)
    print("        DESIGN NOTE for the eventual fix: a unique constraint on plain (user_id, "
          "package_id) would NOT close this path at all, since the two rows above have "
          "DIFFERENT user_ids by construction - any real fix has to serialize/lock on the "
          "telegram_id-wide claim this check actually makes, not on a single user_id.")
finally:
    # Runs on success OR failure - every session closed, every engine
    # disposed (releasing its own pooled connections, not just the one
    # Session wrapping it), THEN the TemporaryDirectory itself removed.
    # Round 3 review found 21 leaked lifecycle_race_* directories from
    # earlier mkdtemp()-without-cleanup runs - this closes that gap for
    # every future run without touching those pre-existing leaked ones
    # (left alone, per instruction, unless Product asks separately).
    for _s in _race_sessions:
        try:
            _s.close()
        except Exception:
            pass
    for _e in _race_engines:
        try:
            _e.dispose()
        except Exception:
            pass
    _race_tmp.cleanup()


# ===========================================================================
if failures:
    print(f"\n{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("\nهمه‌ی تست‌ها گذشت")
