"""Lifecycle/P6 batch L0 - the durable-provisioning SCHEMA: its shape and
the constraints the database really enforces.

Design: docs/lifecycle-provisioning-design-2026-10-01.md (v7.1, frozen),
sections 4, 5 and the L0 row of section 16.

Run:  python3 backend/tests/test_provisioning_schema_l0.py

Proven here against a REAL database (constraints are executed, not just
declared) - SQLite always, and MariaDB whenever MARIADB_TEST_URL points at
a scratch database:

  1. The ten tables / 151 columns of design 5.7, on their own MetaData
     (never part of the project's general create_all / auto-migration).
  2. services/provisioning_schema.bootstrap() creates them, verifies them,
     seeds the two fixed-row tables idempotently and reports ready.
  3. UNIQUE, CHECK and (on MariaDB) FOREIGN KEY ... ON DELETE RESTRICT
     really reject bad rows (schema half of LP-67, LP-68, LP-74, LP-90,
     LP-91, LP-109, LP-190).
  4. The DDL compiled for the MySQL dialect has the expected shape.

The fail-closed / adversarial side (broken schema, wrong seeds, a DDL
failure at startup) is in tests/test_provisioning_schema_guard.py.

MariaDB: in CI (the CI environment variable is set) a missing
MARIADB_TEST_URL is a FAILURE, never a silent skip - .github/workflows/
ci.yml starts a MariaDB service for exactly this. Locally, without the
variable, the real-MariaDB part is skipped and says so.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateIndex, CreateTable

import _mariadb_scratch as scratch
from app import models
from app import models_provisioning as mp
from app.services import provisioning_schema as ps

failures: list[str] = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


NEW_TABLE_NAMES = [t.name for t in mp.PROVISIONING_TABLES]
NOW = dt.datetime(2026, 10, 3, 12, 0, 0)
H64 = "a" * 64


def new_engine(url="sqlite://"):
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    return create_engine(url, **kwargs)


# A violated CHECK is an IntegrityError on SQLite, but MariaDB (4025
# ER_CONSTRAINT_FAILED) and MySQL 8 (3819) report it as an OperationalError.
# Only those two codes count - any other operational error (unknown column,
# syntax, lost connection) is a broken test, not a rejected row.
CHECK_VIOLATION_CODES = (4025, 3819)


def _is_constraint_rejection(exc) -> bool:
    if isinstance(exc, IntegrityError):
        return True
    code = getattr(getattr(exc, "orig", None), "args", [None])[0]
    return isinstance(exc, OperationalError) and code in CHECK_VIOLATION_CODES


def rejected(engine, table, values) -> bool:
    """True if the DATABASE refuses the row, False if it was stored. Each
    attempt is its own transaction, always rolled back."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(table.insert().values(**values))
        except (IntegrityError, OperationalError) as exc:
            trans.rollback()
            if _is_constraint_rejection(exc):
                return True
            raise
        trans.rollback()
        return False


def mariadb_url_or_fail() -> str:
    """The scratch MariaDB URL, or "" when it may legitimately be absent.
    In CI it may not be absent."""
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if not url and os.environ.get("CI"):
        check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
    return url


def drop_provisioning_tables(engine):
    for table in reversed(mp.PROVISIONING_TABLES):
        table.drop(engine, checkfirst=True)


def op_row(**over):
    row = dict(
        operation_type="purchase", business_key=f"req:t:{uuid.uuid4()}", request_hash=H64,
        tenant_scope_key="t", actor_kind="bot", intent="{}", state="prepared",
        forward_deadline=NOW, wallet_epoch_at_start=0, created_at=NOW,
    )
    row.update(over)
    return row


def step_row(operation_id, **over):
    row = dict(
        operation_id=operation_id, slot_key=f"request_slot:{uuid.uuid4().hex[:6]}", direction="create",
        step_order=0, backend="mikrotik_wg", node_id=1, protocol="wireguard", state="staged",
        created_at=NOW, updated_at=NOW,
    )
    row.update(over)
    return row


def reservation_row(operation_id, **over):
    row = dict(
        operation_id=operation_id, payer_kind="reseller_credit", generation="admin_balance",
        admin_id=1, amount=1000, hold_ref=str(uuid.uuid4()), state="reserved", reserved_at=NOW,
    )
    row.update(over)
    return row


VERIFIED = dict(
    adapter_version="v1", server_fingerprint=H64, config_fingerprint=H64, contract='{"not_exist": []}',
    verified_by_admin_id=1, verified_at=NOW,
)


def run_constraint_checks(engine, tag):
    """Everything that must hold on a real database. Used for SQLite always
    and for MariaDB when MARIADB_TEST_URL is provided."""
    ops, steps, res = mp.ProvisioningOperation.__table__, mp.ProvisioningStep.__table__, mp.PaymentReservation.__table__
    claims, rt = mp.OneTimePackageClaim.__table__, mp.ProvisioningRuntimeState.__table__
    contracts, events = mp.ProvisioningNodeContract.__table__, mp.ProvisioningOwnershipEvent.__table__
    modes, recon, stats = (mp.ProvisioningTypeMode.__table__, mp.ProvisioningNodeReconciliation.__table__,
                           mp.ProvisioningGateStat.__table__)

    with engine.begin() as conn:
        op_id = conn.execute(ops.insert().values(**op_row(business_key="base"))).inserted_primary_key[0]

    print(f"--- [{tag}] provisioning_operations ---")
    check(f"[{tag}] valid operation accepted", rejected(engine, ops, op_row()), False)
    check(f"[{tag}] UNIQUE(operation_type, business_key) rejects a duplicate",
          rejected(engine, ops, op_row(business_key="base")))
    check(f"[{tag}] same business_key under another operation_type is allowed",
          rejected(engine, ops, op_row(business_key="base", operation_type="renew_user")), False)
    check(f"[{tag}] unknown operation_type rejected", rejected(engine, ops, op_row(operation_type="nope")))
    check(f"[{tag}] unknown actor_kind rejected", rejected(engine, ops, op_row(actor_kind="alien")))
    check(f"[{tag}] unknown state rejected", rejected(engine, ops, op_row(state="done")))
    check(f"[{tag}] completed without completed_at rejected", rejected(engine, ops, op_row(state="completed")))
    check(f"[{tag}] completed_at on a non-terminal state rejected",
          rejected(engine, ops, op_row(completed_at=NOW)))
    check(f"[{tag}] terminal operation still holding username_claim rejected",
          rejected(engine, ops, op_row(state="compensated", completed_at=NOW, username_claim="u1")))
    approval = dict(approval_uuid=str(uuid.uuid4()), approval_execution_version=3, approval_key_bound=False)
    check(f"[{tag}] approval-bound operation (in-process key) accepted",
          rejected(engine, ops, op_row(**approval)), False)
    check(f"[{tag}] LP-74: approval_uuid without execution version rejected",
          rejected(engine, ops, op_row(approval_uuid=str(uuid.uuid4()), approval_key_bound=False)))
    check(f"[{tag}] LP-74: execution version without approval_uuid rejected",
          rejected(engine, ops, op_row(approval_execution_version=1)))
    check(f"[{tag}] approval without approval_key_bound rejected",
          rejected(engine, ops, op_row(approval_uuid=str(uuid.uuid4()), approval_execution_version=1)))
    check(f"[{tag}] approval_key_bound=1 without a key instance uuid rejected",
          rejected(engine, ops, op_row(approval_uuid=str(uuid.uuid4()), approval_execution_version=1,
                                       approval_key_bound=True)))
    check(f"[{tag}] key instance uuid with approval_key_bound=0 rejected",
          rejected(engine, ops, op_row(approval_uuid=str(uuid.uuid4()), approval_execution_version=1,
                                       approval_key_bound=False, execution_key_instance_uuid=str(uuid.uuid4()))))
    check(f"[{tag}] approval on a delete_* operation rejected",
          rejected(engine, ops, op_row(operation_type="delete_user", **approval)))
    check(f"[{tag}] optional_effects without an approval rejected",
          rejected(engine, ops, op_row(optional_effects="[]", state="completed", completed_at=NOW)))
    check(f"[{tag}] optional_effects before 'completed' rejected",
          rejected(engine, ops, op_row(optional_effects="[]", **approval)))
    with engine.begin() as conn:
        conn.execute(ops.insert().values(**op_row(business_key="a1", username_claim="taken", **approval)))
    check(f"[{tag}] LP-74: second operation for the same (approval_uuid, version) rejected, even of another type",
          rejected(engine, ops, op_row(operation_type="create_user", **approval)))
    check(f"[{tag}] same approval_uuid with the NEXT version accepted",
          rejected(engine, ops, op_row(**dict(approval, approval_execution_version=4))), False)
    check(f"[{tag}] UNIQUE(username_claim) rejects a second holder of the name",
          rejected(engine, ops, op_row(username_claim="taken")))
    with engine.begin() as conn:
        conn.execute(ops.insert().values(**op_row(business_key="n1")))
    check(f"[{tag}] two operations without approval / without username_claim coexist (NULL-exempt uniques)",
          rejected(engine, ops, op_row(business_key="n2")), False)

    print(f"--- [{tag}] provisioning_steps ---")
    check(f"[{tag}] valid staged step accepted", rejected(engine, steps, step_row(op_id)), False)
    with engine.begin() as conn:
        conn.execute(steps.insert().values(**step_row(op_id, slot_key="slot:dup")))
    check(f"[{tag}] UNIQUE(operation_id, slot_key) rejects a duplicate slot",
          rejected(engine, steps, step_row(op_id, slot_key="slot:dup")))
    for backend in mp.STEP_BACKENDS:
        check(f"[{tag}] backend {backend} accepted", rejected(engine, steps, step_row(op_id, backend=backend)), False)
    check(f"[{tag}] unknown backend rejected", rejected(engine, steps, step_row(op_id, backend="openvpn_x")))
    check(f"[{tag}] unknown direction rejected", rejected(engine, steps, step_row(op_id, direction="sideways")))
    check(f"[{tag}] unknown step state rejected", rejected(engine, steps, step_row(op_id, state="done")))
    check(f"[{tag}] unknown remote_outcome rejected",
          rejected(engine, steps, step_row(op_id, state="removed", remote_outcome="gone")))
    secrets = dict(staged_wg_private_key="k", staged_password="p", staged_xr_uuid="u")
    check(f"[{tag}] staged step may hold all three credentials",
          rejected(engine, steps, step_row(op_id, **secrets)), False)
    for column in secrets:
        check(f"[{tag}] LP-109: 'removed' still holding {column} rejected",
              rejected(engine, steps, step_row(op_id, state="removed", **{column: "x"})))
        check(f"[{tag}] LP-109: 'active' still holding {column} rejected",
              rejected(engine, steps, step_row(op_id, state="active", connection_id=5, **{column: "x"})))
    for state in ("compensating", "cleanup_required"):
        for column in ("staged_wg_private_key", "staged_password"):
            check(f"[{tag}] LP-109: '{state}' still holding {column} rejected",
                  rejected(engine, steps, step_row(op_id, state=state, remote_attempted=True, **{column: "x"})))
        check(f"[{tag}] '{state}' may keep staged_xr_uuid (needed to match the remote identity)",
              rejected(engine, steps, step_row(op_id, state=state, remote_attempted=True, staged_xr_uuid="u")), False)
    check(f"[{tag}] 'removed' with no remote attempt and no outcome accepted",
          rejected(engine, steps, step_row(op_id, state="removed")), False)
    check(f"[{tag}] 'removed' after a remote attempt but with no outcome rejected",
          rejected(engine, steps, step_row(op_id, state="removed", remote_attempted=True)))
    for outcome in ("verified_absent", "delete_idempotently_absent"):
        check(f"[{tag}] 'removed' with {outcome} accepted",
              rejected(engine, steps, step_row(op_id, state="removed", remote_attempted=True,
                                               remote_outcome=outcome)), False)
    check(f"[{tag}] 'removed' with 'unverified' rejected",
          rejected(engine, steps, step_row(op_id, state="removed", remote_attempted=True,
                                           remote_outcome="unverified")))
    forced = dict(forced_by_admin_id=1, force_reason="host gone", forced_at=NOW)
    check(f"[{tag}] 'abandoned' with full force audit accepted",
          rejected(engine, steps, step_row(op_id, state="removed", remote_attempted=True,
                                           remote_outcome="abandoned", **forced)), False)
    check(f"[{tag}] 'abandoned' without force audit rejected",
          rejected(engine, steps, step_row(op_id, state="removed", remote_attempted=True,
                                           remote_outcome="abandoned")))
    check(f"[{tag}] force audit without 'abandoned' rejected",
          rejected(engine, steps, step_row(op_id, state="removed", remote_attempted=True,
                                           remote_outcome="verified_absent", **forced)))
    check(f"[{tag}] forced_by_admin_id without force_reason rejected",
          rejected(engine, steps, step_row(op_id, state="removed", remote_attempted=True, remote_outcome="abandoned",
                                           forced_by_admin_id=1, forced_at=NOW)))
    check(f"[{tag}] 'cleanup_required' with 'unverified' accepted",
          rejected(engine, steps, step_row(op_id, state="cleanup_required", remote_attempted=True,
                                           remote_outcome="unverified")), False)
    check(f"[{tag}] 'cleanup_required' with 'verified_absent' rejected",
          rejected(engine, steps, step_row(op_id, state="cleanup_required", remote_attempted=True,
                                           remote_outcome="verified_absent")))
    check(f"[{tag}] remote_outcome on a 'staged' step rejected",
          rejected(engine, steps, step_row(op_id, remote_outcome="verified_absent")))
    check(f"[{tag}] 'remote_calling' without remote_attempted rejected",
          rejected(engine, steps, step_row(op_id, state="remote_calling")))
    check(f"[{tag}] 'remote_calling' with remote_attempted accepted",
          rejected(engine, steps, step_row(op_id, state="remote_calling", remote_attempted=True)), False)
    check(f"[{tag}] a 'remove' step may be 'compensating' before any remote attempt",
          rejected(engine, steps, step_row(op_id, direction="remove", state="compensating")), False)
    check(f"[{tag}] radius_ppp step with remote_attempted rejected",
          rejected(engine, steps, step_row(op_id, backend="radius_ppp", state="remote_calling",
                                           remote_attempted=True)))
    check(f"[{tag}] 'active' without connection_id rejected", rejected(engine, steps, step_row(op_id, state="active")))
    check(f"[{tag}] 'active' with connection_id accepted",
          rejected(engine, steps, step_row(op_id, state="active", connection_id=7)), False)

    print(f"--- [{tag}] payment_reservations ---")
    check(f"[{tag}] valid reseller reservation accepted", rejected(engine, res, reservation_row(op_id)), False)
    with engine.begin() as conn:
        conn.execute(res.insert().values(**reservation_row(op_id, hold_ref="hold-1")))
    check(f"[{tag}] UNIQUE(operation_id, payer_kind) rejects a second reseller reservation",
          rejected(engine, res, reservation_row(op_id)))
    wallet = dict(payer_kind="customer_wallet", generation="legacy_balance", admin_id=None, user_id=9)
    check(f"[{tag}] customer wallet reservation on the same operation accepted",
          rejected(engine, res, reservation_row(op_id, **wallet)), False)
    check(f"[{tag}] UNIQUE(hold_ref) rejects a reused hold_ref",
          rejected(engine, res, reservation_row(op_id, hold_ref="hold-1", **wallet)))
    check(f"[{tag}] amount = 0 rejected", rejected(engine, res, reservation_row(op_id, amount=0, **wallet)))
    check(f"[{tag}] negative amount rejected", rejected(engine, res, reservation_row(op_id, amount=-5, **wallet)))
    check(f"[{tag}] customer_wallet with admin_balance generation rejected",
          rejected(engine, res, reservation_row(op_id, **dict(wallet, generation="admin_balance"))))
    check(f"[{tag}] customer_wallet without user_id rejected",
          rejected(engine, res, reservation_row(op_id, **dict(wallet, user_id=None))))
    check(f"[{tag}] reseller_credit carrying a user_id rejected",
          rejected(engine, res, reservation_row(op_id, user_id=3)))
    check(f"[{tag}] 'captured' without ledger entry rejected",
          rejected(engine, res, reservation_row(op_id, state="captured", captured_at=NOW, **wallet)))
    check(f"[{tag}] 'captured' with ledger entry and captured_at accepted",
          rejected(engine, res, reservation_row(op_id, state="captured", captured_at=NOW,
                                                capture_ledger_entry_id=4, **wallet)), False)
    check(f"[{tag}] 'released' carrying a ledger entry rejected",
          rejected(engine, res, reservation_row(op_id, state="released", released_at=NOW,
                                                capture_ledger_entry_id=4, **wallet)))
    check(f"[{tag}] 'reserved' with released_at rejected",
          rejected(engine, res, reservation_row(op_id, released_at=NOW, **wallet)))
    check(f"[{tag}] unknown reservation state rejected",
          rejected(engine, res, reservation_row(op_id, state="pending", **wallet)))

    print(f"--- [{tag}] one_time_package_claims ---")
    user_claim = dict(package_id=1, claim_key="b" * 64, claim_kind="user", claim_user_id=5,
                      operation_id=op_id, created_at=NOW)
    with engine.begin() as conn:
        claim_id = conn.execute(claims.insert().values(**user_claim)).inserted_primary_key[0]
    check(f"[{tag}] second ACTIVE claim for the same (package, claim_key) rejected",
          rejected(engine, claims, user_claim))
    with engine.begin() as conn:
        conn.execute(claims.update().where(claims.c.id == claim_id).values(release_seq=claim_id, released_at=NOW))
    check(f"[{tag}] after release, a new active claim for the same key is accepted",
          rejected(engine, claims, user_claim), False)
    check(f"[{tag}] LP-91: a different claim_key on the same package is accepted",
          rejected(engine, claims, dict(user_claim, claim_key="c" * 64)), False)
    check(f"[{tag}] LP-90: claim_key shorter than 64 rejected",
          rejected(engine, claims, dict(user_claim, claim_key="c" * 63)))
    check(f"[{tag}] released_at without release_seq rejected",
          rejected(engine, claims, dict(user_claim, claim_key="d" * 64, released_at=NOW)))
    telegram = dict(package_id=1, claim_key="e" * 64, claim_kind="telegram", claim_tenant_scope_key="t" * 64,
                    claim_telegram_id=9223372036854775807, operation_id=op_id, created_at=NOW)
    check(f"[{tag}] LP-90: telegram claim with max-length tenant and max BIGINT telegram id accepted",
          rejected(engine, claims, telegram), False)
    check(f"[{tag}] telegram claim carrying claim_user_id rejected",
          rejected(engine, claims, dict(telegram, claim_user_id=1)))
    check(f"[{tag}] user claim without claim_user_id rejected",
          rejected(engine, claims, dict(user_claim, claim_key="f" * 64, claim_user_id=None)))
    check(f"[{tag}] unknown claim_kind rejected",
          rejected(engine, claims, dict(user_claim, claim_key="f" * 64, claim_kind="phone")))

    print(f"--- [{tag}] provisioning_runtime_state ---")
    base_rt = dict(id=1, installation_uuid=str(uuid.uuid4()), gate_mode_changed_at=NOW, changed_at=NOW)
    check(f"[{tag}] a second singleton row (id = 2) rejected", rejected(engine, rt, dict(base_rt, id=2)))
    with engine.begin() as conn:
        conn.execute(rt.delete())
    check(f"[{tag}] inert default singleton accepted", rejected(engine, rt, base_rt), False)
    owner = dict(owner_state="active", owner_host_id=H64, owner_boot_id=str(uuid.uuid4()),
                 owner_claimed_at=NOW, owner_heartbeat_at=NOW)
    locked = dict(lock_backend="flock", lock_verified_at=NOW)
    check(f"[{tag}] gate_mode 'shadow' while lock_backend is 'none' rejected",
          rejected(engine, rt, dict(base_rt, gate_mode="shadow")))
    check(f"[{tag}] lock_backend set without lock_verified_at rejected",
          rejected(engine, rt, dict(base_rt, lock_backend="flock")))
    check(f"[{tag}] 'enforced' without an owner rejected",
          rejected(engine, rt, dict(base_rt, gate_mode="enforced", **locked)))
    check(f"[{tag}] 'enforced' with verified lock and an active owner accepted",
          rejected(engine, rt, dict(base_rt, gate_mode="enforced", **locked, **owner)), False)
    check(f"[{tag}] owner_state 'active' without owner_host_id rejected",
          rejected(engine, rt, dict(base_rt, owner_state="active")))
    check(f"[{tag}] owner_host_id while owner_state is 'released' rejected",
          rejected(engine, rt, dict(base_rt, **dict(owner, owner_state="released"))))
    check(f"[{tag}] owner without boot id rejected",
          rejected(engine, rt, dict(base_rt, **dict(owner, owner_boot_id=None))))
    check(f"[{tag}] unknown gate_mode rejected", rejected(engine, rt, dict(base_rt, gate_mode="strict", **locked)))
    check(f"[{tag}] unknown lock_backend rejected",
          rejected(engine, rt, dict(base_rt, lock_backend="redis", lock_verified_at=NOW)))

    print(f"--- [{tag}] provisioning_node_contracts (LP-190) ---")
    check(f"[{tag}] bare 'unverified' row accepted",
          rejected(engine, contracts, dict(node_id=1, backend="marzban", state="unverified")), False)
    for column, value in list(VERIFIED.items()) + [("invalidated_at", NOW), ("invalidation_reason", "manual")]:
        check(f"[{tag}] 'unverified' carrying {column} rejected",
              rejected(engine, contracts, dict(node_id=1, backend="sui", state="unverified", **{column: value})))
    check(f"[{tag}] full 'ready' row accepted",
          rejected(engine, contracts, dict(node_id=2, backend="marzban", state="ready", **VERIFIED)), False)
    for column in VERIFIED:
        check(f"[{tag}] 'ready' missing {column} rejected",
              rejected(engine, contracts, dict(node_id=2, backend="sui", state="ready",
                                               **dict(VERIFIED, **{column: None}))))
    check(f"[{tag}] 'ready' carrying invalidation fields rejected",
          rejected(engine, contracts, dict(node_id=2, backend="sui", state="ready", invalidated_at=NOW,
                                           invalidation_reason="manual", **VERIFIED)))
    check(f"[{tag}] 'invalidated' keeping its verification snapshot accepted",
          rejected(engine, contracts, dict(node_id=3, backend="marzban", state="invalidated", invalidated_at=NOW,
                                           invalidation_reason="config_changed", **VERIFIED)), False)
    check(f"[{tag}] 'invalidated' without invalidated_at rejected",
          rejected(engine, contracts, dict(node_id=3, backend="sui", state="invalidated",
                                           invalidation_reason="manual", **VERIFIED)))
    check(f"[{tag}] 'invalidated' without its verification snapshot rejected",
          rejected(engine, contracts, dict(node_id=3, backend="sui", state="invalidated", invalidated_at=NOW,
                                           invalidation_reason="manual")))
    check(f"[{tag}] unknown invalidation_reason rejected",
          rejected(engine, contracts, dict(node_id=3, backend="sui", state="invalidated", invalidated_at=NOW,
                                           invalidation_reason="because", **VERIFIED)))
    check(f"[{tag}] radius_ppp never has a contract row",
          rejected(engine, contracts, dict(node_id=4, backend="radius_ppp", state="unverified")))
    with engine.begin() as conn:
        conn.execute(contracts.insert().values(node_id=5, backend="hiddify", state="unverified"))
    check(f"[{tag}] PRIMARY KEY (node_id, backend) rejects a duplicate",
          rejected(engine, contracts, dict(node_id=5, backend="hiddify", state="unverified")))
    check(f"[{tag}] same node, another backend, is an independent row",
          rejected(engine, contracts, dict(node_id=5, backend="sui", state="unverified")), False)

    print(f"--- [{tag}] provisioning_ownership_events ---")
    event = dict(event_type="claim", ownership_epoch=1, actor_admin_id=1, created_at=NOW)
    check(f"[{tag}] plain claim event accepted", rejected(engine, events, event), False)
    for needs_reason in mp.OWNERSHIP_EVENTS_REQUIRING_REASON:
        check(f"[{tag}] {needs_reason} without a reason rejected",
              rejected(engine, events, dict(event, event_type=needs_reason)))
        check(f"[{tag}] {needs_reason} with a reason accepted",
              rejected(engine, events, dict(event, event_type=needs_reason, reason="why")), False)
    check(f"[{tag}] gate_mode_change without gate_mode_to rejected",
          rejected(engine, events, dict(event, event_type="gate_mode_change")))
    check(f"[{tag}] gate_mode_to on a non gate_mode_change event rejected",
          rejected(engine, events, dict(event, gate_mode_to="shadow")))
    check(f"[{tag}] unknown event_type rejected", rejected(engine, events, dict(event, event_type="hijack")))

    print(f"--- [{tag}] small tables ---")
    check(f"[{tag}] type mode 'durable' accepted",
          rejected(engine, modes, dict(operation_type="x_probe", mode="durable", changed_at=NOW)), False)
    check(f"[{tag}] unknown type mode rejected",
          rejected(engine, modes, dict(operation_type="x_probe", mode="hybrid", changed_at=NOW)))
    check(f"[{tag}] unknown reconciliation result rejected",
          rejected(engine, recon, dict(node_id=1, last_run_at=NOW, result="fine", db_subnet="10.0.0.0/24")))
    with engine.begin() as conn:
        conn.execute(stats.insert().values(node_id=0, updated_at=NOW))
        stored = conn.execute(text("SELECT node_id FROM provisioning_gate_stats")).scalar()
    check(f"[{tag}] gate_stats row node_id = 0 is stored as 0 (not renumbered by AUTO_INCREMENT)", stored, 0)

    if engine.dialect.name != "sqlite":
        # SQLite in this project does not enforce foreign keys at all
        # (database.py sets no PRAGMA foreign_keys) - which is exactly why
        # nothing outside these tables is referenced by FK. On MariaDB the
        # three internal FKs are real.
        print(f"--- [{tag}] foreign keys, ON DELETE RESTRICT ---")
        check(f"[{tag}] step pointing at a non-existent operation rejected",
              rejected(engine, steps, step_row(987654321)))
        check(f"[{tag}] reservation pointing at a non-existent operation rejected",
              rejected(engine, res, reservation_row(987654321)))
        check(f"[{tag}] claim pointing at a non-existent operation rejected",
              rejected(engine, claims, dict(user_claim, claim_key="9" * 64, operation_id=987654321)))
        restricted = None
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(ops.delete().where(ops.c.id == op_id))
                restricted = False
            except IntegrityError:
                restricted = True
            trans.rollback()
        check(f"[{tag}] deleting an operation that still has steps/reservations/claims is RESTRICTed", restricted)


# ---------------------------------------------------------------------------
print("--- shape: ten tables, 151 columns (design 5.7) ---")
check("exactly ten provisioning tables are declared", len(mp.PROVISIONING_TABLES), 10)
check("column count per table matches design 5.7",
      {t.name: len(t.columns) for t in mp.PROVISIONING_TABLES},
      {
          "provisioning_operations": 28, "provisioning_steps": 38, "payment_reservations": 14,
          "provisioning_type_modes": 4, "one_time_package_claims": 12, "provisioning_runtime_state": 15,
          "provisioning_node_reconciliations": 7, "provisioning_node_contracts": 12,
          "provisioning_ownership_events": 10, "provisioning_gate_stats": 11,
      })
check("total column count", sum(len(t.columns) for t in mp.PROVISIONING_TABLES), 151)
check("none of the ten is in the project's general metadata (create_all / auto-migrate never see them)",
      sorted(set(NEW_TABLE_NAMES) & set(models.Base.metadata.tables)), [])
check("they share one separate MetaData",
      {t.metadata is mp.PROVISIONING_METADATA for t in mp.PROVISIONING_TABLES}, {True})
check("no name collides with a pre-existing table", sorted(set(NEW_TABLE_NAMES) & set(models.Base.metadata.tables)), [])
check("no provisioning table has a foreign key to a pre-existing table",
      sorted({fk.column.table.name for t in mp.PROVISIONING_TABLES for fk in t.foreign_keys}),
      ["provisioning_operations"])
check("no pre-existing table has a foreign key into a provisioning table",
      [t.name for t in models.Base.metadata.tables.values()
       if t.name not in NEW_TABLE_NAMES and any(fk.column.table.name in NEW_TABLE_NAMES for fk in t.foreign_keys)],
      [])
check("every CHECK and UNIQUE constraint is named (the inspector matches by name)",
      [f"{t.name}:{c!r}" for t in mp.PROVISIONING_TABLES for c in t.constraints
       if c.__class__.__name__ in ("CheckConstraint", "UniqueConstraint") and not c.name],
      [])

def run_bootstrap_checks(engine, tag):
    Session = sessionmaker(bind=engine)
    result = ps.bootstrap(engine, Session)
    check(f"[{tag}] bootstrap creates the tables and reports ready", (result["ready"], result["problems"]), (True, []))
    check(f"[{tag}] all ten tables exist in the database",
          sorted(set(NEW_TABLE_NAMES) - set(inspect(engine).get_table_names())), [])
    check(f"[{tag}] inspector finds no difference", ps.inspect_schema(engine), [])
    check(f"[{tag}] is_ready() / assert_ready() agree", (ps.is_ready(), ps.assert_ready()), (True, None))
    db = Session()
    runtime = db.get(mp.ProvisioningRuntimeState, 1)
    check(f"[{tag}] singleton seeded at its inert defaults",
          (runtime.lock_backend, runtime.gate_mode, runtime.owner_state, runtime.gate_mode_epoch,
           runtime.ownership_epoch, runtime.owner_host_id),
          ("none", "off", "none", 0, 0, None))
    check(f"[{tag}] installation_uuid is a canonical uuid",
          str(uuid.UUID(runtime.installation_uuid)), runtime.installation_uuid)
    first_uuid = runtime.installation_uuid
    check(f"[{tag}] one 'legacy' row per operation_type",
          sorted((m.operation_type, m.mode) for m in db.query(mp.ProvisioningTypeMode)),
          sorted((t, "legacy") for t in mp.OPERATION_TYPES))
    db.close()
    again = ps.bootstrap(engine, Session)
    db = Session()
    check(f"[{tag}] a second bootstrap is ready too (idempotent)", again["ready"], True)
    check(f"[{tag}] ...and never rewrites installation_uuid",
          db.get(mp.ProvisioningRuntimeState, 1).installation_uuid, first_uuid)
    check(f"[{tag}] ...still exactly eight type-mode rows and one singleton",
          (db.query(mp.ProvisioningTypeMode).count(), db.query(mp.ProvisioningRuntimeState).count()), (8, 1))
    check(f"[{tag}] L0 seeds nothing else",
          [t.name for t in mp.PROVISIONING_TABLES
           if t.name not in ("provisioning_runtime_state", "provisioning_type_modes")
           and db.execute(text(f"SELECT COUNT(*) FROM {t.name}")).scalar() != 0],
          [])
    db.close()


print("--- real SQLite: bootstrap, inspector, seeds ---")
run_bootstrap_checks(new_engine(), "sqlite")

constraint_engine = new_engine()
ps.create_tables(constraint_engine)
run_constraint_checks(constraint_engine, "sqlite")

print("--- MariaDB/MySQL: compiled DDL ---")
my = mysql.dialect()
ddl = {t.name: str(CreateTable(t).compile(dialect=my)) for t in mp.PROVISIONING_TABLES}
check("every table compiles for the MySQL dialect", sorted(ddl), sorted(NEW_TABLE_NAMES))
check("every named CHECK constraint is in the MySQL DDL",
      [f"{t.name}:{c.name}" for t in mp.PROVISIONING_TABLES for c in t.constraints
       if c.__class__.__name__ == "CheckConstraint" and f"CONSTRAINT {c.name} CHECK" not in ddl[t.name]],
      [])
check("every named UNIQUE constraint is in the MySQL DDL",
      [f"{t.name}:{c.name}" for t in mp.PROVISIONING_TABLES for c in t.constraints
       if c.__class__.__name__ == "UniqueConstraint" and f"CONSTRAINT {c.name} UNIQUE" not in ddl[t.name]],
      [])
check("timestamps are DATETIME(6) on MySQL (microseconds)",
      "forward_deadline DATETIME(6) NOT NULL" in ddl["provisioning_operations"])
check("...and plain DATETIME on SQLite",
      "forward_deadline DATETIME NOT NULL" in str(CreateTable(mp.ProvisioningOperation.__table__)
                                                 .compile(dialect=sqlite.dialect())))
check("surrogate ids are BIGINT AUTO_INCREMENT on MySQL",
      [name for name in ("provisioning_operations", "provisioning_steps", "payment_reservations",
                         "one_time_package_claims", "provisioning_ownership_events")
       if "id BIGINT NOT NULL AUTO_INCREMENT" not in ddl[name]],
      [])
check("the singleton and per-node keys are NOT AUTO_INCREMENT "
      "(MariaDB rejects a CHECK on an AUTO_INCREMENT column; node_id 0 must stay 0)",
      [name for name in ("provisioning_runtime_state", "provisioning_node_reconciliations",
                         "provisioning_node_contracts", "provisioning_gate_stats", "provisioning_type_modes")
       if "AUTO_INCREMENT" in ddl[name]],
      [])
check("foreign keys to provisioning_operations are ON DELETE RESTRICT",
      [name for name in ("provisioning_steps", "payment_reservations", "one_time_package_claims")
       if "REFERENCES provisioning_operations (id) ON DELETE RESTRICT" not in ddl[name]],
      [])
check("the composite primary key of node contracts",
      "PRIMARY KEY (node_id, backend)" in ddl["provisioning_node_contracts"])
check("no TEXT column carries a DEFAULT (MySQL/MariaDB reject it)",
      [name for name, sql in ddl.items() if any("TEXT" in line and "DEFAULT" in line for line in sql.splitlines())],
      [])
index_ddl = [str(CreateIndex(i).compile(dialect=my)) for t in mp.PROVISIONING_TABLES for i in t.indexes]
check("all seven secondary indexes compile for MySQL", len(index_ddl), 7)
check("unique index keys stay under InnoDB's 3072-byte limit at utf8mb4 (4 bytes/char)",
      [c.name for t in mp.PROVISIONING_TABLES for c in t.constraints
       if c.__class__.__name__ == "UniqueConstraint"
       and sum(4 * (getattr(col.type, "length", None) or 2) for col in c.columns) > 3072],
      [])

mariadb_url = mariadb_url_or_fail()
if mariadb_url:
    print("--- real MariaDB (MARIADB_TEST_URL) ---")
    try:
        maria = scratch.claim(mariadb_url)
    except scratch.ScratchRefused as refused:
        maria = None
        check(f"MARIADB_TEST_URL must point at a throwaway database ({refused})", False)
    if maria is not None:
        try:
            scratch.wipe(maria)
            run_bootstrap_checks(maria, "mariadb")
            # A clean set of tables for the row-level checks (bootstrap seeded
            # the singleton and the type modes, which those checks insert).
            drop_provisioning_tables(maria)
            ps.create_tables(maria)
            check("[mariadb] inspector finds no difference on freshly created tables", ps.inspect_schema(maria), [])
            run_constraint_checks(maria, "mariadb")
        finally:
            scratch.release(maria)
elif not os.environ.get("CI"):
    print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI) - "
          "locally MariaDB is covered by compiled DDL only")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
