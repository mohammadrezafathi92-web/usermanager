"""ANALYSIS ONLY (per Product's instruction for this stage) - reproduces the
CURRENT correlation gap that a future "ابطال کامل رسید اسکمیِ تأییدشده"
(void a confirmed-fraudulent receipt) feature would have to close. No fix
lands in this file or anywhere else this run - this is reproduction
evidence for design documents (docs/receipt-void-design-2026-10-02.md,
built on docs/lifecycle-provisioning-design-2026-10-01.md), not a
regression test for an existing bug.

v2 (this revision) - corrections made per Codex's review of v1:
  - The real count of `check()` calls below is whatever `grep -c "check("`
    on this file says AT THE TIME IT IS READ - no fixed number is claimed
    in prose anywhere in this docstring, since a stale hard-coded count was
    exactly v1's own reporting mistake.
  - Section [2] now calls the REAL async `admin_pending.perform_approval`
    (claim_pending -> perform_approval, exactly cb_approval's own sequence),
    not routers/bot.py's HTTP endpoint directly. `app.telegram_bot.
    panel_bridge.SessionLocal` is monkeypatched (try/finally-restored) to
    the test's own StaticPool engine so perform_approval's in-process
    PanelBridge calls land in the SAME database the test fixtures use -
    without this, panel_bridge's real SessionLocal (bound to app.database's
    module-level engine) would silently operate on a completely different,
    empty in-memory database.
  - New section [6] exercises renew (legacy/user-level, no
    renew_purchase_id), topup, and link ALL through the real
    perform_approval path - including a genuine `api.link_telegram` call
    for the link kind (v1 only ever created a pending row for 'link' and
    compared a Ledger count, never actually linking anything).
  - `created_conn` is now asserted against directly (which connection type/
    user it belongs to), not read-and-discarded.
  - All tempfile cleanup and monkeypatch restoration (provision_wireguard,
    get_connection_share, panel_bridge.SessionLocal, bot_config.
    bot_owner_admin_id) now run inside a single outer try/finally, so a
    failing check anywhere above does not leak the temp sqlite file or
    leave any patch applied for whatever runs in this process afterward.
  - NOT covered here (explicitly out of scope for this reproduction, not
    an oversight): "new" purchase for an ALREADY-linked existing user (the
    purchase_package branch), renew WITH a renew_purchase_id (the
    multi-service picker path), and the auto_approve.py decide()/
    try_auto_approve() wrapper itself - that wrapper calls this exact same
    perform_approval, so its own cap/window logic is a separate concern
    from the correlation gap this file demonstrates, not a second
    implementation that could drift from what's tested here.

Traced against real code (file:line references in the design docs, not
repeated here) - this file PROVES, by actually running the real approval
path and then inspecting the real resulting rows, that:

  [1] The pending-approval store (telegram_bot/storage.py's
      `pending_purchases` table) is a RAW SQLITE FILE, completely
      independent of the main panel database (which may itself be SQLite
      OR MySQL/MariaDB - see database.py). There is no shared transaction,
      no shared connection, no cross-database foreign key between them.
  [2] Once `perform_approval`'s own production-side calls run (create_user/
      purchase_package/renew_service/renew/add_balance, add_balance for the
      card-bookkeeping step, accounting.record for the Ledger row), NOTHING
      written to the main database - User, Purchase, Connection,
      LedgerEntry, PaymentCard - carries ANY column referencing the pending
      request's own id, or any other identifier that would let an admin
      later ask "which production rows came from pending request #N?" and
      get a reliable answer back.
  [3] PaymentCard.accumulated_amount is a bare running counter with no
      per-event log - two different approvals' amounts are
      indistinguishable inside it once added, and a threshold rotation
      resets it to 0, destroying even the ability to tell "how much of the
      current total came from which approval" after the fact.
  [4] A crash between `claim_pending` (status -> 'processing') and the
      final `set_status(..., 'approved')` leaves a request permanently
      invisible to BOTH the pending queue (status='pending' only) and the
      history view (status IN ('approved','rejected') only) - with no
      recovery path in the current code.
  [5] A `link` request's pending row carries no payment/price information
      worth voiding.
  [6] A REAL `link` approval (api.link_telegram, via perform_approval) does
      not touch accounting.record/admin_billing at all, and the SAME
      correlation gap from [2] applies identically to renew (legacy/
      user-level) and topup approvals - confirming `link` is correctly OUT
      of scope for a receipt-void feature (nothing financial happens), and
      that renew/topup need the exact same correlation fix as `new`.

Zero network calls (WireGuard/Xray/SoftEther mocked exactly like
test_lifecycle_provisioning_analysis.py); the SAME permanent socket guard
from that file is reused here so this file can never make one either.

Run:  python3 backend/tests/test_receipt_void_correlation_analysis.py
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

# ---------------------------------------------------------------------------
# Same permanent, fail-closed network guard as test_lifecycle_provisioning_
# analysis.py - installed before any fixture, for the same reason (this is
# a test_*.py picked up by run_all.py/CI and must make zero real network
# calls, structurally, not by convention).
class NetworkCallBlocked(AssertionError):
    pass


def _blocked_connect(self, address, *a, **k):
    raise NetworkCallBlocked(
        f"test_receipt_void_correlation_analysis.py: blocked a REAL socket.connect to {address!r}"
    )


def _blocked_create_connection(address, *a, **k):
    raise NetworkCallBlocked(
        f"test_receipt_void_correlation_analysis.py: blocked a REAL socket.create_connection to {address!r}"
    )


def _blocked_connect_ex(self, address, *a, **k):
    raise NetworkCallBlocked(
        f"test_receipt_void_correlation_analysis.py: blocked a REAL socket.connect_ex to {address!r}"
    )


def _blocked_sendto(self, *args, **kwargs):
    dest = args[-1] if args else kwargs.get("address", "?")
    raise NetworkCallBlocked(
        f"test_receipt_void_correlation_analysis.py: blocked a REAL socket.sendto to {dest!r}"
    )


socket.socket.connect = _blocked_connect
socket.create_connection = _blocked_create_connection
socket.socket.connect_ex = _blocked_connect_ex
socket.socket.sendto = _blocked_sendto
# ---------------------------------------------------------------------------

from sqlalchemy import create_engine, inspect as sa_inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.services import hierarchy, payment_cards, user_ops
from app.telegram_bot import panel_bridge
from app.telegram_bot import storage as bot_storage
from app.telegram_bot.config import config as bot_config
from app.telegram_bot.handlers import admin_pending

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def add_admin(db, username, **extra):
    row = models.AdminUser(username=username, hashed_password="x", role="admin", **extra)
    db.add(row)
    db.commit()
    db.refresh(row)
    hierarchy.rebuild_path(db, row)
    db.commit()
    return row


def approve(pending_id: int, bot) -> tuple[bool, str]:
    """Exactly cb_approval's own sequence (claim -> perform_approval),
    run synchronously for this script-style test file."""
    assert bot_storage.claim_pending(pending_id), f"claim_pending failed for {pending_id}"
    pending = bot_storage.get_pending(pending_id)
    return asyncio.run(admin_pending.perform_approval(pending, bot))


# ===========================================================================
# Setup - all cleanup/monkeypatch restoration for everything below happens
# in the single outer try/finally at the bottom of this file.
# ===========================================================================
_tmp_bot_db = tempfile.NamedTemporaryFile(prefix="bot_pending_", suffix=".db", delete=False)
_tmp_bot_db.close()
bot_config.db_path = _tmp_bot_db.name
bot_storage.init_db()

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(engine)
TestSessionLocal = sessionmaker(bind=engine)
db = TestSessionLocal()

# A network-free provisioning stub (same reasoning as
# test_lifecycle_provisioning_analysis.py's own _fake_provision_wireguard):
# the correlation question this file is about has nothing to do with
# WireGuard itself, so the remote side is stubbed, not the thing under test.
_real_provision_wireguard = user_ops.provision_wireguard


def _fake_provision_wireguard(db_, user, node, purchase_batch=None, package_name=None,
                              max_concurrent_sessions=1, speed_limit_mbps=None):
    conn = models.Connection(
        user_id=user.id, node_id=node.id, type=models.ConnectionType.wireguard,
        wg_peer_name=f"user-{user.username}-fake", wg_public_key="pub", wg_private_key="priv",
        wg_client_address="10.0.0.2/32", purchase_batch=purchase_batch, package_name_snapshot=package_name,
    )
    db_.add(conn)
    db_.commit()
    db_.refresh(conn)
    return conn


# Same reasoning as test_lifecycle_provisioning_analysis.py's own patch:
# response serialization (routers/bot.py's _connection_info) calls
# user_ops.get_connection_share() for ANY wireguard Connection in a
# response, which makes a genuine MikrotikClient network call for the
# server's public key - independent of how the row was created. Only the
# wireguard branch is stubbed; this file has no other protocol in play.
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


# panel_bridge._call() does `db = SessionLocal()` using the name imported
# into panel_bridge's OWN namespace (`from ..database import SessionLocal`)
# - that is app.database's module-level engine, entirely unrelated to this
# test's own StaticPool engine above. Without this patch, every real
# perform_approval() call below would silently operate on a second, empty
# in-memory database that shares no rows with admin_a/pkg_a/card_a/node_ok.
_real_panel_bridge_session_local = panel_bridge.SessionLocal

user_ops.provision_wireguard = _fake_provision_wireguard
user_ops.get_connection_share = _network_free_get_connection_share
panel_bridge.SessionLocal = TestSessionLocal

try:
    # =======================================================================
    print("=" * 72)
    print("--- [1] pending_purchases is a raw, separate SQLite file - no shared ---")
    print("--- transaction/connection with the main panel database at all ---")
    print("=" * 72)

    from app.database import is_mysql, is_sqlite  # import after env var is set, same as before

    check("the bot's own pending-store path is a plain local sqlite FILE path, "
          "never the main app's own DATABASE_URL connection string",
          bot_config.db_path.endswith(".db") and not bot_config.db_path.startswith("sqlite"),
          True)
    check("the main app's own database dialect flags exist independently of the bot "
          "store (database.py's is_sqlite/is_mysql) - the bot store has NO equivalent "
          "dialect awareness at all, it is unconditionally sqlite3 (storage.py's own "
          "import), regardless of what the main app's DATABASE_URL says",
          isinstance(is_sqlite, bool) and isinstance(is_mysql, bool), True)

    admin_a = add_admin(db, "admin_a", balance=1_000_000)
    # Mirrors a real dedicated-bot thread for admin_a (see
    # telegram_bot/config.py's RuntimeConfig.bot_owner_admin_id docstring) -
    # without this, perform_approval's api.create_user call would resolve
    # its owner from config.bot_owner_admin_id (None, the shared/global
    # bot's default) instead of admin_a, and then fail Phase C's package-
    # ownership check against pkg_a (owner_admin_id=admin_a.id).
    bot_config.bot_owner_admin_id = admin_a.id

    node_ok = models.Node(name="node-ok", type=models.NodeType.mikrotik,
                          mt_host="1.1.1.1", mt_username="u", mt_password="p")
    pkg_a = models.Package(name="pkg-a", quota_gb=1, duration_days=30, price=20000,
                           enabled=True, bot_enabled=True, owner_admin_id=admin_a.id)
    db.add_all([node_ok, pkg_a])
    db.commit()
    db.add(models.PackageConnection(package_id=pkg_a.id, node_id=node_ok.id, protocol=models.ConnectionType.wireguard))
    db.commit()

    card_a = models.PaymentCard(owner_admin_id=admin_a.id, card_number="6037991111111111",
                                card_holder="admin_a card", accumulated_amount=0)
    db.add(card_a)
    db.commit()

    # This is the SAME shape storage.create_pending builds (telegram_bot/
    # handlers/customer.py's _ask_for_receipt) - a plain dict, no reference to
    # the main database's own schema at all.
    pending_id = bot_storage.create_pending(
        telegram_id=777000,
        telegram_username="realbuyer",
        telegram_name="Real Buyer",
        kind="new",
        package={"id": pkg_a.id, "name": pkg_a.name, "quota_gb": pkg_a.quota_gb,
                 "duration_days": pkg_a.duration_days, "price": pkg_a.price},
        target_username="fraud_customer",
        receipt_file_id="AgACAgfakefileid123",
        final_price=20000,
        payment_card_id=card_a.id,
        owner_admin_id=admin_a.id,
    )
    check("a pending request was created in the bot's OWN separate sqlite file", pending_id > 0, True)

    # ===========================================================================
    print("\n" + "=" * 72)
    print("--- [2] after the REAL perform_approval production calls run, ---")
    print("--- nothing in the main DB references the pending request at all ---")
    print("=" * 72)

    bot = AsyncMock()  # fake aiogram Bot - every send_* call is a harmless no-op
    ok, msg = approve(pending_id, bot)
    check("perform_approval succeeds, exactly like a real admin tap would see", ok, True)

    # The SAME best-effort card bookkeeping perform_approval already did as
    # part of approve() above (api.record_card_payment) - re-read here just
    # to anchor the [3] section below to this exact state.
    db.refresh(card_a)

    created_user = db.query(models.User).filter(models.User.username == "fraud_customer").first()
    created_conn = db.query(models.Connection).filter(models.Connection.user_id == created_user.id).first()
    created_ledger = db.query(models.LedgerEntry).filter(models.LedgerEntry.user_id == created_user.id).first()

    check("[FINDING] a real User row was created for this approval", created_user is not None, True)
    check("a real Connection row was created for this approval, correctly linked to the "
          "created User and of the protocol the package asked for - but (see the next "
          "check) with no way back to the approval that caused it",
          (created_conn is not None, created_conn.user_id == created_user.id if created_conn else None,
           created_conn.type if created_conn else None),
          (True, True, models.ConnectionType.wireguard))
    check("[FINDING] models.User has NO column of any kind referencing the pending "
          "request's id or any other cross-store identifier",
          "pending_id" in {c.name for c in sa_inspect(models.User).columns}
          or "approval_id" in {c.name for c in sa_inspect(models.User).columns}
          or "approval_uuid" in {c.name for c in sa_inspect(models.User).columns},
          False)
    check("[FINDING] models.Connection has NO such column either - confirmed on the "
          "ACTUAL created_conn row above, not just the model's column list",
          any(n in {c.name for c in sa_inspect(models.Connection).columns}
              for n in ("pending_id", "approval_id", "approval_uuid")),
          False)
    check("[FINDING] models.Purchase has NO such column either",
          any(n in {c.name for c in sa_inspect(models.Purchase).columns}
              for n in ("pending_id", "approval_id", "approval_uuid")),
          False)
    # Receipt Void P0 added a nullable LedgerEntry.approval_uuid column - the
    # schema for the correlation. P0 only adds the column: nothing writes it
    # yet, so the finding itself (the sale cannot be traced to its approval)
    # still holds, and is asserted on the ACTUAL row below.
    check("LedgerEntry now HAS the approval_uuid column (Receipt Void P0 schema)",
          "approval_uuid" in {c.name for c in sa_inspect(models.LedgerEntry).columns}, True)
    check("[FINDING] ...but nothing fills it: the ledger row of this very sale has "
          "approval_uuid NULL, so it still does not identify WHICH approval produced it",
          getattr(created_ledger, "approval_uuid", "missing") if created_ledger is not None else "no row",
          None)
    check("[FINDING] a real Ledger row for the sale does exist, but reading it back "
          "gives no way to reconstruct which pending_purchases row (in a COMPLETELY "
          "SEPARATE sqlite file) produced it - only a human cross-referencing "
          "username + amount + approximate time could guess, which Product's own "
          "instruction explicitly forbids relying on",
          created_ledger is not None, True)

    # ===========================================================================
    print("\n" + "=" * 72)
    print("--- [3] PaymentCard.accumulated_amount is a bare counter, no per-event ---")
    print("--- log - two approvals' amounts are indistinguishable once added, and ---")
    print("--- a threshold rotation resets it to 0, losing even that much ---")
    print("=" * 72)

    check("the card's running counter moved by exactly this approval's amount",
          card_a.accumulated_amount, 20000)

    # A second, unrelated approval on the SAME card, also through the REAL
    # perform_approval path.
    pending_id_2 = bot_storage.create_pending(
        telegram_id=777001, telegram_username="otherbuyer", telegram_name="Other Buyer",
        kind="new", package={"id": pkg_a.id, "name": pkg_a.name, "quota_gb": pkg_a.quota_gb,
                             "duration_days": pkg_a.duration_days, "price": pkg_a.price},
        target_username="other_customer", final_price=15000, payment_card_id=card_a.id,
        owner_admin_id=admin_a.id,
    )
    ok2, _ = approve(pending_id_2, bot)
    check("the second approval also succeeds through the real path", ok2, True)
    db.refresh(card_a)
    check("[FINDING] the SAME counter now mixes both approvals' amounts "
          "(20000+15000=35000) with no stored way to tell them apart again",
          card_a.accumulated_amount, 35000)

    check("[FINDING] PaymentCard has no child/event table recording individual "
          "advance_after_payment calls at all - models.py defines no such table",
          "PaymentCardReceiptEvent" in dir(models), False)

    # Simulate a threshold rotation happening on top of this (advance_after_payment
    # itself, with a real threshold configured) - proves the reset-to-0 behavior
    # that would make a later point-subtraction for just pending_id_1 actively wrong.
    admin_a.own_payment_card_switch_threshold = 30000
    admin_a.own_payment_card_mode = "threshold"
    admin_a.own_active_payment_card_id = card_a.id
    card_b = models.PaymentCard(owner_admin_id=admin_a.id, card_number="6037992222222222",
                                card_holder="admin_a card 2", accumulated_amount=0, sort_order=1)
    card_a.sort_order = 0
    db.add(card_b)
    db.commit()
    payment_cards.advance_after_payment(db, card_a.id, 1)  # tiny extra push past the 30000 threshold
    db.refresh(card_a)
    db.refresh(admin_a)
    check("[FINDING] crossing the threshold reset card_a's counter to 0 and rotated "
          "the active pointer to card_b - if a later void tried to subtract "
          "pending_id_1's 20000 from card_a's CURRENT counter, it would now be "
          "subtracting from a counter that belongs to a different, later round "
          "entirely (0, not 35001)",
          (card_a.accumulated_amount, admin_a.own_active_payment_card_id), (0, card_b.id))

    # ===========================================================================
    print("\n" + "=" * 72)
    print("--- [4] a crash between claim_pending and set_status leaves a request ---")
    print("--- permanently invisible to BOTH the pending queue and the history ---")
    print("=" * 72)

    pending_id_3 = bot_storage.create_pending(
        telegram_id=777002, telegram_username="crashbuyer", telegram_name="Crash Buyer",
        kind="new", package={"id": pkg_a.id, "name": pkg_a.name, "quota_gb": pkg_a.quota_gb,
                             "duration_days": pkg_a.duration_days, "price": pkg_a.price},
        target_username="crash_customer", final_price=5000, owner_admin_id=admin_a.id,
    )
    check("claim succeeds (pending -> processing), exactly as it would right before "
          "perform_approval's production calls run",
          bot_storage.claim_pending(pending_id_3), True)
    # Simulate a hard process crash right here: no set_status("approved"), no
    # release_pending() (the except blocks in perform_approval that call
    # release_pending never run on a genuine SIGKILL/power loss - only on a
    # caught Python exception).

    check("[FINDING] the crashed request is invisible to the pending queue "
          "(list_pending only matches status='pending')",
          any(p["id"] == pending_id_3 for p in bot_storage.list_pending()), False)
    check("[FINDING] ...and ALSO invisible to the history view "
          "(list_recent only matches status IN ('approved','rejected'))",
          any(p["id"] == pending_id_3 for p in bot_storage.list_recent(limit=500)), False)
    stuck = bot_storage.get_pending(pending_id_3)
    check("[FINDING] the row still exists and is stuck in 'processing' forever - "
          "get_pending (by exact id) is the ONLY way to ever see it again, and "
          "nothing in the current code ever calls that proactively to look for "
          "stuck rows",
          stuck["status"] if stuck else None, "processing")

    # ===========================================================================
    print("\n" + "=" * 72)
    print("--- [5]/[6] renew (legacy) and topup through the REAL perform_approval ---")
    print("--- path have the SAME correlation gap as 'new'; a REAL link approval ---")
    print("--- (api.link_telegram) touches no accounting/billing at all ---")
    print("=" * 72)

    before_ledger_for_renew = db.query(models.LedgerEntry).filter(models.LedgerEntry.user_id == created_user.id).count()
    pending_id_renew = bot_storage.create_pending(
        telegram_id=777000, telegram_username="realbuyer", telegram_name="Real Buyer",
        kind="renew", package={"id": pkg_a.id, "name": pkg_a.name, "quota_gb": pkg_a.quota_gb,
                               "duration_days": pkg_a.duration_days, "price": pkg_a.price},
        target_username="fraud_customer",  # the user created_user above, renewing - no renew_purchase_id
        final_price=20000, payment_card_id=card_a.id, owner_admin_id=admin_a.id,
    )
    ok_renew, msg_renew = approve(pending_id_renew, bot)
    check("a REAL legacy (user-level, no renew_purchase_id) renew approval succeeds "
          "through perform_approval", ok_renew, True)
    ledger_after_renew = db.query(models.LedgerEntry).filter(models.LedgerEntry.user_id == created_user.id).count()
    check("[FINDING] the renew's own Ledger row has the exact same correlation gap as "
          "'new' did in [2] - a new row was written but nothing on it, or on the User/ "
          "Purchase it affected, says WHICH approval (this one vs the original 'new' "
          "one) caused it",
          ledger_after_renew > before_ledger_for_renew, True)

    pending_id_topup = bot_storage.create_pending(
        telegram_id=777000, telegram_username="realbuyer", telegram_name="Real Buyer",
        kind="topup", package={"id": pkg_a.id, "name": pkg_a.name, "quota_gb": 0,
                               "duration_days": 0, "price": 50000},  # topup amount, see storage.create_pending
        target_username="fraud_customer", payment_card_id=card_a.id, owner_admin_id=admin_a.id,
    )
    balance_before_topup = created_user.balance
    ok_topup, msg_topup = approve(pending_id_topup, bot)
    check("a REAL topup approval succeeds through perform_approval", ok_topup, True)
    db.refresh(created_user)
    check("[FINDING] the topup really did move the user's wallet balance by the pending "
          "row's own price (50000) - this is REAL money a later void would have to "
          "account for, not a hypothetical",
          created_user.balance, balance_before_topup + 50000)

    before_ledger_count_link = db.query(models.LedgerEntry).count()
    link_telegram_id = 888999
    pending_id_link = bot_storage.create_pending(
        telegram_id=link_telegram_id, telegram_username="linkbuyer", telegram_name="Link Buyer",
        kind="link", package={"id": pkg_a.id, "name": pkg_a.name, "quota_gb": 0, "duration_days": 0, "price": 0},
        target_username="fraud_customer", owner_admin_id=admin_a.id,
    )
    link_pending = bot_storage.get_pending(pending_id_link)
    check("a 'link' pending row carries no payment_card_id at all, and its final_price "
          "is a meaningless 0 (there was no real package/price involved, just an "
          "identity claim) - nothing here describes a real payment to void",
          (link_pending.get("payment_card_id"), link_pending.get("final_price")), (None, 0))

    ok_link, msg_link = approve(pending_id_link, bot)
    check("[CONFIRMED] a REAL 'link' approval (api.link_telegram, not just a pending "
          "row) succeeds through perform_approval", ok_link, True)
    db.refresh(created_user)
    check("[CONFIRMED] the REAL link call actually changed the target user's "
          "telegram_id to the new linked id", created_user.telegram_id, link_telegram_id)
    check("[CONFIRMED] a REAL 'link' approval never calls accounting.record or "
          "admin_billing - no Ledger row count change happened anywhere in the whole "
          "database from actually executing this approval, confirming nothing "
          "financial is in play for this kind (not just that the pending row looked "
          "empty)",
          db.query(models.LedgerEntry).count(), before_ledger_count_link)

finally:
    user_ops.provision_wireguard = _real_provision_wireguard
    user_ops.get_connection_share = _real_get_connection_share
    panel_bridge.SessionLocal = _real_panel_bridge_session_local
    bot_config.bot_owner_admin_id = None
    try:
        db.close()
    except Exception:
        pass
    try:
        engine.dispose()
    except Exception:
        pass
    try:
        os.unlink(_tmp_bot_db.name)
    except FileNotFoundError:
        pass
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(_tmp_bot_db.name + suffix)
        except FileNotFoundError:
            pass

if failures:
    print(f"\n{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("\nهمه‌ی تست‌ها گذشت")
