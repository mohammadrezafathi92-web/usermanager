"""Receipt Void phase P0 - schema, seeds and key-identity readiness.

Design: docs/receipt-void-design-2026-10-02.md (v16.1, frozen), sections
4, 5, 7-11, 14.2, 15 and the P0 row of section 21.
Run:  python3 backend/tests/test_receipt_void_schema_p0.py

Against a REAL database - SQLite always, MariaDB when MARIADB_TEST_URL is
set (mandatory in CI, see test_provisioning_schema_l0.py):

  1. 36 new tables on their own MetaData; ten nullable columns and two
     explicit unique indexes on three existing tables.
  2. bootstrap(): create -> inspect -> seed -> validate -> key identity;
     idempotent; never fatal; fail-closed.
  3. Money- and identity-critical constraints really reject bad rows.
  4. An existing database is upgraded by the real startup sequence without
     touching existing data (RV-278, RV-283).
  5. Key-identity readiness: backfill, missing index, duplicates.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from app import main, models
from app import models_receipt_void as rv
from app.services import provisioning_schema as ps, receipt_void_schema as rvs

failures: list[str] = []
NOW = dt.datetime(2026, 10, 4, 12, 0, 0)
H64 = "a" * 64
NAMES = [t.name for t in rv.RECEIPT_VOID_TABLES]


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def new_engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False})


def rejected(engine, table, values) -> bool:
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(table.insert().values(**values))
        except (IntegrityError, OperationalError) as exc:
            trans.rollback()
            code = getattr(getattr(exc, "orig", None), "args", [None])[0]
            if isinstance(exc, IntegrityError) or code in (4025, 3819):
                return True
            raise
        trans.rollback()
        return False


def insert(engine, table, **values):
    with engine.begin() as conn:
        return conn.execute(table.insert().values(**values)).inserted_primary_key[0]


def drop_all(engine):
    for table in reversed(rv.RECEIPT_VOID_TABLES):
        table.drop(engine, checkfirst=True)


print("--- shape ---")
check("36 tables", len(rv.RECEIPT_VOID_TABLES), 36)
check("398 columns", sum(len(t.columns) for t in rv.RECEIPT_VOID_TABLES), 398)
check("none of them is in the project's general metadata", sorted(set(NAMES) & set(models.Base.metadata.tables)), [])
check("every CHECK and UNIQUE is named",
      [t.name for t in rv.RECEIPT_VOID_TABLES for c in t.constraints
       if c.__class__.__name__ in ("CheckConstraint", "UniqueConstraint") and not c.name], [])
check("constraint and index names are unique and at most 64 characters",
      (lambda names: (len(names) == len(set(names)), max(len(n) for n in names) <= 64))(
          [c.name for t in rv.RECEIPT_VOID_TABLES for c in list(t.constraints) + list(t.indexes) if c.name]),
      (True, True))
check("every CHECK expression is parseable by the schema inspector",
      [c.name for t in rv.RECEIPT_VOID_TABLES for c in t.constraints
       if c.__class__.__name__ == "CheckConstraint" and ps.normalize_check(str(c.sqltext)) is None], [])
check("foreign keys leave these tables only toward users, ledger_entries and payment_cards",
      sorted({fk.column.table.name for t in rv.RECEIPT_VOID_TABLES for fk in t.foreign_keys} - set(NAMES)),
      ["ledger_entries", "payment_cards", "users"])
check("the twelve effect types of the design", len(rv.EFFECT_TYPES), 12)
check("the ten additive columns are declared on the three existing models, all nullable",
      sorted((table, column, models.Base.metadata.tables[table].c[column].nullable)
             for table, columns in rvs.ADDED_COLUMNS.items() for column in columns),
      sorted((table, column, True) for table, columns in rvs.ADDED_COLUMNS.items() for column in columns))
check("...ten of them", sum(len(c) for c in rvs.ADDED_COLUMNS.values()), 10)
check("the two unique indexes are explicit Index objects (so the additive migration creates them)",
      sorted((t, i.name, i.unique) for t in ("api_keys", "ledger_entries")
             for i in models.Base.metadata.tables[t].indexes if i.name.startswith("uq_")),
      [("api_keys", "uq_api_keys_key_instance_uuid", True), ("ledger_entries", "uq_ledger_entries_reversal_of_id", True)])
check("a new ApiKey gets an instance uuid by default",
      len(str(uuid.UUID(models.ApiKey.__table__.c.key_instance_uuid.default.arg(None)))), 36)


def run_bootstrap_checks(engine, tag):
    Session = sessionmaker(bind=engine)
    outcome = rvs.bootstrap(engine, Session)
    check(f"[{tag}] bootstrap is ready", (outcome["ready"], outcome["problems"]), (True, []))
    check(f"[{tag}] all 36 tables exist", sorted(set(NAMES) - set(inspect(engine).get_table_names())), [])
    check(f"[{tag}] inspector finds no difference", ps.inspect_schema(engine, rv.RECEIPT_VOID_TABLES), [])
    with engine.connect() as conn:
        approval = conn.execute(select(rv.receipt_approval_runtime_state)).mappings().one()
        wallet = conn.execute(select(rv.wallet_runtime_state)).mappings().one()
    check(f"[{tag}] approval runtime singleton is inert",
          (approval["registration_mode"], approval["auto_rate_limit_mode"], approval["loyalty_timing_mode"],
           approval["completeness_generation"]), ("off", "off", "legacy_activation", 0))
    check(f"[{tag}] wallet runtime singleton is inert", (wallet["phase"], wallet["epoch"], wallet["external_fencing"]),
          ("normal", 0, "none"))
    check(f"[{tag}] key identity was computed and is ready (no API keys, index present)",
          (bool(approval["key_identity_ready"]), approval["key_identity_failure"], approval["key_identity_checked_at"] is not None),
          (True, None, True))
    version_before = approval["version"]
    again = rvs.bootstrap(engine, Session)
    with engine.connect() as conn:
        counts = [conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                  for t in ("receipt_approval_runtime_state", "wallet_runtime_state")]
        seeded_elsewhere = [t for t in NAMES if t not in ("receipt_approval_runtime_state", "wallet_runtime_state")
                            and conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()]
    check(f"[{tag}] a second bootstrap is ready and adds no row", (again["ready"], counts), (True, [1, 1]))
    check(f"[{tag}] P0 seeds nothing else", seeded_elsewhere, [])
    check(f"[{tag}] is_ready()/assert_ready() agree", (rvs.is_ready(), rvs.assert_ready()), (True, None))
    return version_before


def approval_row(**over):
    row = dict(
        approval_uuid=str(uuid.uuid4()), pending_source_instance_id="inst", pending_local_id=1,
        immutable_intent_hash=H64, manifest_hash=H64, registered_under_mode="shadow",
        kind="new", target_shape="new_user", approval_mode="manual", original_approval_mode="manual",
        original_approved_by_telegram_id=5, original_approver_evidence_kind="linked_admin",
        approved_by_telegram_id=5, approver_evidence_kind="linked_admin",
        auto_approve_policy_snapshot="{}", tenant_scope_key="t", telegram_id_snapshot=99,
        telegram_id_source="bot_attested", target_username_snapshot="u", amount_snapshot=1000,
        state="registered", created_at=NOW,
    )
    row.update(over)
    return row


def run_constraint_checks(engine, tag):
    A, O = rv.receipt_approvals, rv.wallet_operations
    print(f"--- [{tag}] receipt_approvals ---")
    check(f"[{tag}] a valid manual approval for a new user is accepted", rejected(engine, A, approval_row()), False)
    base_uuid = str(uuid.uuid4())
    insert(engine, A, **approval_row(approval_uuid=base_uuid, pending_local_id=7))
    check(f"[{tag}] UNIQUE(approval_uuid)", rejected(engine, A, approval_row(approval_uuid=base_uuid, pending_local_id=8)))
    check(f"[{tag}] UNIQUE(pending instance, pending id, registration_seq)",
          rejected(engine, A, approval_row(pending_local_id=7)))
    check(f"[{tag}] manual without approver rejected",
          rejected(engine, A, approval_row(approved_by_telegram_id=None, approver_evidence_kind=None)))
    check(f"[{tag}] approver without evidence kind rejected", rejected(engine, A, approval_row(approver_evidence_kind=None)))
    check(f"[{tag}] unknown evidence kind rejected", rejected(engine, A, approval_row(approver_evidence_kind="friend")))
    check(f"[{tag}] auto approval for a topup rejected (topup is never auto)",
          rejected(engine, A, approval_row(approval_mode="auto", approved_by_telegram_id=None, approver_evidence_kind=None,
                                           auto_granted_at=NOW, kind="topup", target_shape="topup",
                                           target_user_id_snapshot=3, telegram_id_source="user_row")))
    check(f"[{tag}] auto approval without auto_granted_at rejected",
          rejected(engine, A, approval_row(approval_mode="auto", approved_by_telegram_id=None, approver_evidence_kind=None)))
    check(f"[{tag}] new_user carrying a target user id rejected",
          rejected(engine, A, approval_row(target_user_id_snapshot=4)))
    check(f"[{tag}] existing_user without a target user id rejected",
          rejected(engine, A, approval_row(target_shape="existing_user", telegram_id_source="user_row")))
    check(f"[{tag}] new_user whose telegram id is not bot-attested rejected",
          rejected(engine, A, approval_row(telegram_id_source="user_row")))
    check(f"[{tag}] 'required' registration with completeness_generation 0 rejected",
          rejected(engine, A, approval_row(registered_under_mode="required")))
    check(f"[{tag}] 'shadow' registration with a completeness_generation rejected",
          rejected(engine, A, approval_row(completeness_generation=1)))
    check(f"[{tag}] cancelled without a reason rejected", rejected(engine, A, approval_row(state="cancelled")))
    check(f"[{tag}] a reason on a non-cancelled approval rejected",
          rejected(engine, A, approval_row(cancellation_reason="username_taken")))
    check(f"[{tag}] registration_seq > 0 without supersedes rejected", rejected(engine, A, approval_row(registration_seq=1)))
    check(f"[{tag}] unknown state rejected", rejected(engine, A, approval_row(state="done")))

    print(f"--- [{tag}] effects and manifest ---")
    E, X = rv.receipt_approval_effects, rv.receipt_approval_expected_effects
    effect = dict(approval_uuid=base_uuid, effect_type="ledger_sale", effect_key="sale", resource_type="LedgerEntry",
                  resource_id=1, actual_projection="{}", resource_snapshot="{}", created_at=NOW)
    insert(engine, E, **effect)
    check(f"[{tag}] the same (approval, effect_type, effect_key) twice rejected", rejected(engine, E, effect))
    check(f"[{tag}] unknown effect_type rejected", rejected(engine, E, dict(effect, effect_type="magic", effect_key="k2")))
    check(f"[{tag}] reversal_state without voided_at/void_operation rejected",
          rejected(engine, E, dict(effect, effect_key="k3", reversal_state="fully_reversed", reversed_value=5)))
    check(f"[{tag}] reversed_value without a reversal_state rejected",
          rejected(engine, E, dict(effect, effect_key="k4", reversed_value=5)))
    check(f"[{tag}] manifest: unknown requirement rejected",
          rejected(engine, X, dict(approval_uuid=base_uuid, effect_type="ledger_sale", effect_key="sale",
                                   requirement="maybe", expected="{}", created_at=NOW)))

    print(f"--- [{tag}] operations ---")
    operation = dict(operation_type="void_receipt", business_key=base_uuid, request_hash=H64, tenant_scope_key="t",
                     actor_kind="admin", state="pending", epoch_at_start=0, result_snapshot="{}", created_at=NOW)
    op_id = insert(engine, O, **operation)
    check(f"[{tag}] UNIQUE(operation_type, business_key): one void operation per approval", rejected(engine, O, operation))
    check(f"[{tag}] unknown operation_type rejected", rejected(engine, O, dict(operation, operation_type="hack", business_key="b")))
    S = rv.wallet_operation_steps
    step = dict(wallet_operation_id=op_id, step_key="verify:1", step_order=1, result_snapshot="{}", created_at=NOW)
    check(f"[{tag}] step 'blocked' needs remote_outcome 'unverified'", rejected(engine, S, dict(step, state="blocked")))
    check(f"[{tag}] step 'done' may not be 'unverified' (an unverified delete never counts as done)",
          rejected(engine, S, dict(step, state="done", remote_outcome="unverified")))
    check(f"[{tag}] step 'done' with verified_absent accepted",
          rejected(engine, S, dict(step, state="done", remote_outcome="verified_absent")), False)

    print(f"--- [{tag}] wallet: identities, accounts, lots, debits ---")
    identity = insert(engine, rv.customer_identities, tenant_scope_key="t", identity_key="tg:99", created_at=NOW)
    check(f"[{tag}] UNIQUE(tenant, identity_key)",
          rejected(engine, rv.customer_identities, dict(tenant_scope_key="t", identity_key="tg:99", created_at=NOW)))
    account_row = dict(lineage_key=str(uuid.uuid4()), customer_identity_id=identity, user_id=None, user_id_snapshot=1,
                       username_snapshot="u", tombstoned_at=NOW, created_at=NOW)
    account = insert(engine, rv.wallet_accounts, **account_row)
    check(f"[{tag}] an account that is neither live nor tombstoned rejected",
          rejected(engine, rv.wallet_accounts, dict(account_row, lineage_key=str(uuid.uuid4()), tombstoned_at=None)))
    check(f"[{tag}] two tombstoned accounts (user_id NULL) coexist (NULL-exempt unique)",
          rejected(engine, rv.wallet_accounts, dict(account_row, lineage_key=str(uuid.uuid4()))), False)
    L = rv.wallet_lots
    lot = dict(wallet_account_id=account, source_kind="legacy_baseline", source_key="baseline", original_amount=100,
               available_amount=100, consumed_amount=0, state="active", created_at=NOW)
    lot_id = insert(engine, L, **lot)
    check(f"[{tag}] UNIQUE(account, source_kind, source_key)", rejected(engine, L, lot))
    check(f"[{tag}] conservation: available + consumed must equal original",
          rejected(engine, L, dict(lot, source_key="x1", available_amount=60, consumed_amount=30)))
    check(f"[{tag}] an 'exhausted' lot with money left rejected",
          rejected(engine, L, dict(lot, source_key="x2", state="exhausted")))
    check(f"[{tag}] a 'voided' lot without voided_available_amount rejected",
          rejected(engine, L, dict(lot, source_key="x3", state="voided", voided_at=NOW, available_amount=0)))
    check(f"[{tag}] a non-baseline lot without its credit source rejected",
          rejected(engine, L, dict(lot, source_key="x4", source_kind="receipt_topup")))
    check(f"[{tag}] a negative available amount rejected",
          rejected(engine, L, dict(lot, source_key="x5", available_amount=-1, consumed_amount=101)))
    D = rv.wallet_debit_events
    debit_op = insert(engine, O, **dict(operation, operation_type="wallet_debit", business_key="d1"))
    debit = dict(wallet_account_id=account, kind="sale", amount=50, hold_ref=str(uuid.uuid4()), state="held",
                 held_at=NOW, wallet_operation_id=debit_op, created_at=NOW)
    check(f"[{tag}] a held sale debit is accepted", rejected(engine, D, debit), False)
    check(f"[{tag}] a 'captured' sale without its LedgerEntry rejected (no capture without a recorded sale)",
          rejected(engine, D, dict(debit, state="captured")))
    check(f"[{tag}] a manual adjustment without actor and reason rejected",
          rejected(engine, D, dict(debit, kind="manual_adjustment", state="captured")))
    check(f"[{tag}] amount 0 rejected", rejected(engine, D, dict(debit, amount=0)))
    insert(engine, D, **debit)
    other_op = insert(engine, O, **dict(operation, operation_type="wallet_debit", business_key="d2"))
    check(f"[{tag}] UNIQUE(hold_ref)", rejected(engine, D, dict(debit, wallet_operation_id=other_op)))
    W = rv.wallet_debts
    debt = dict(customer_identity_id=identity, wallet_account_id=account, source_wallet_lot_id=lot_id,
                source_void_operation_id=op_id, amount=100, state="open", created_at=NOW)
    check(f"[{tag}] an open debt is accepted", rejected(engine, W, debt), False)
    check(f"[{tag}] 'settled' while money is still owed rejected", rejected(engine, W, dict(debt, state="settled")))
    check(f"[{tag}] settled_amount above what is owed rejected",
          rejected(engine, W, dict(debt, state="partially_settled", settled_amount=150)))

    print(f"--- [{tag}] singletons, loyalty, cards ---")
    R = rv.receipt_approval_runtime_state
    ready = dict(key_identity_ready=True, updated_at=NOW)
    check(f"[{tag}] a second approval-runtime row rejected", rejected(engine, R, dict(ready, id=2)))
    with engine.begin() as conn:
        conn.execute(R.delete())
    check(f"[{tag}] 'required' without payment_event loyalty rejected (one switch, not two)",
          rejected(engine, R, dict(ready, id=1, registration_mode="required", completeness_generation=1)))
    check(f"[{tag}] 'required' with generation 0 rejected",
          rejected(engine, R, dict(ready, id=1, registration_mode="required", loyalty_timing_mode="payment_event")))
    check(f"[{tag}] rate limit 'enforced' while registration is not 'required' rejected",
          rejected(engine, R, dict(ready, id=1, auto_rate_limit_mode="enforced")))
    check(f"[{tag}] key_identity_ready together with a failure rejected",
          rejected(engine, R, dict(ready, id=1, key_identity_failure="duplicate_uuid")))
    check(f"[{tag}] NOT ready is storable in any mode (no CHECK ties mode to readiness)",
          rejected(engine, R, dict(id=1, registration_mode="required", loyalty_timing_mode="payment_event",
                                   completeness_generation=1, key_identity_ready=False,
                                   key_identity_failure="check_error", updated_at=NOW)), False)
    check(f"[{tag}] a second wallet-runtime row rejected",
          rejected(engine, rv.wallet_runtime_state, dict(id=2, updated_at=NOW)))
    P = rv.payment_card_pool_events
    insert(engine, rv.payment_card_pool_states, pool_key="global", logging_phase="event_logged", updated_at=NOW)
    card_event = dict(pool_key="global", event_kind="payment_recorded", card_id_snapshot=1, approval_uuid=base_uuid,
                      amount=500, accumulated_before=0, card_order_snapshot="[1]", mode_snapshot="manual", created_at=NOW)
    insert(engine, P, **card_event)
    check(f"[{tag}] one card payment per approval (UNIQUE approval_uuid)", rejected(engine, P, card_event))
    check(f"[{tag}] a correlated event without approval_uuid rejected",
          rejected(engine, P, dict(card_event, approval_uuid=None)))
    check(f"[{tag}] reset_after outside threshold mode rejected",
          rejected(engine, P, dict(card_event, approval_uuid=str(uuid.uuid4()), reset_after=True)))
    check(f"[{tag}] quota shortfall must name exactly one source",
          rejected(engine, rv.quota_reversal_shortfalls,
                   dict(wallet_operation_id=op_id, user_id_snapshot=1, granted_bytes=10, reversed_bytes=4,
                        shortfall_bytes=6, created_at=NOW)))
    check(f"[{tag}] loyalty epoch with a negative threshold rejected",
          rejected(engine, rv.loyalty_policy_epochs,
                   dict(threshold=-1, credit_amount=0, quota_bytes=0, generation=1, policy_version=1, started_at=NOW)))

    if engine.dialect.name != "sqlite":
        print(f"--- [{tag}] foreign keys ---")
        check(f"[{tag}] an effect for a non-existent approval rejected",
              rejected(engine, E, dict(effect, approval_uuid=str(uuid.uuid4()), effect_key="fk1")))
        check(f"[{tag}] a step for a non-existent operation rejected", rejected(engine, S, dict(step, wallet_operation_id=987654321, state="pending")))


print("--- real SQLite ---")
sqlite_engine = new_engine()
models.Base.metadata.create_all(sqlite_engine)
run_bootstrap_checks(sqlite_engine, "sqlite")
constraint_engine = new_engine()
models.Base.metadata.create_all(constraint_engine)
rvs.create_tables(constraint_engine)
run_constraint_checks(constraint_engine, "sqlite")

print("--- startup: an existing database is upgraded, existing data untouched (RV-278, RV-283) ---")


def run_startup(engine):
    capture = []
    handler = logging.Handler(level=logging.ERROR)
    handler.emit = capture.append
    logging.getLogger().addHandler(handler)
    saved = (main.engine, main.SessionLocal)
    main.engine, main.SessionLocal = engine, sessionmaker(bind=engine)
    raised = None
    try:
        main._create_schema()
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    finally:
        main.engine, main.SessionLocal = saved
        logging.getLogger().removeHandler(handler)
    return raised, capture


old = new_engine()
models.Base.metadata.create_all(old)
with old.begin() as conn:
    # Make it look like a database from before P0: the new columns and the
    # two unique indexes do not exist, and there are keys and ledger rows.
    conn.execute(text("DROP INDEX uq_api_keys_key_instance_uuid"))
    conn.execute(text("DROP INDEX uq_ledger_entries_reversal_of_id"))
    for table, columns in rvs.ADDED_COLUMNS.items():
        for name in ("ix_ledger_entries_approval_uuid", "ix_discount_code_redemptions_approval_uuid"):
            conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
        for column in columns:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    conn.execute(text("INSERT INTO api_keys (label, key, enabled, key_type, scope_enforced) "
                      "VALUES ('k1', 'secret-one', 1, 'legacy_global', 0), ('k2', 'secret-two', 1, 'legacy_global', 0)"))
    conn.execute(text("INSERT INTO ledger_entries (kind, amount) VALUES ('sale_new', 1500)"))
check("the simulated old database really lacks the P0 columns", rvs.inspect_added_columns(old) != [], True)
raised, errors = run_startup(old)
check("startup on the old database does not raise", raised, None)
check("...and logs no error", [r.getMessage() for r in errors], [])
check("the ten columns and both unique indexes now exist", rvs.inspect_added_columns(old), [])
check("receipt void is ready", (rvs.is_ready(), rvs.status()["problems"]), (True, []))
with old.connect() as conn:
    key_uuids = [row[0] for row in conn.execute(text("SELECT key_instance_uuid FROM api_keys ORDER BY id"))]
    ledger = conn.execute(text("SELECT kind, amount, approval_uuid, reversal_of_id, voided_at FROM ledger_entries")).all()
    readiness = conn.execute(select(rv.receipt_approval_runtime_state.c.key_identity_ready,
                                    rv.receipt_approval_runtime_state.c.key_identity_failure)).one()
check("RV-278: both old API keys were given distinct canonical instance uuids",
      (len(set(key_uuids)), all(str(uuid.UUID(u)) == u for u in key_uuids)), (2, True))
check("RV-278: key identity is ready", (bool(readiness[0]), readiness[1]), (True, None))
check("the existing ledger row is untouched; its new columns are NULL", [tuple(r) for r in ledger],
      [("sale_new", 1500, None, None, None)])
with old.begin() as conn:
    conn.execute(text("INSERT INTO ledger_entries (kind, amount, reversal_of_id) VALUES ('receipt_void', 0, 1)"))
check("RV-283: the unique index on reversal_of_id really allows only one reversal per row",
      rejected(old, models.LedgerEntry.__table__, dict(kind="receipt_void", amount=0, reversal_of_id=1)))
raised, errors = run_startup(old)
with old.connect() as conn:
    again = [row[0] for row in conn.execute(text("SELECT key_instance_uuid FROM api_keys ORDER BY id"))]
check("a second startup changes nothing: the uuids are stable", again, key_uuids)

print("--- key identity failure modes are durable and do not make the schema 'not ready' ---")
Session = sessionmaker(bind=old)
with old.begin() as conn:
    conn.execute(text("DROP INDEX uq_api_keys_key_instance_uuid"))
check("unique index missing -> failure 'unique_index_missing'", rvs.compute_key_identity(old, Session), "unique_index_missing")
with old.connect() as conn:
    stored = conn.execute(select(rv.receipt_approval_runtime_state.c.key_identity_ready,
                                 rv.receipt_approval_runtime_state.c.key_identity_failure)).one()
check("...written to the singleton", (bool(stored[0]), stored[1]), (False, "unique_index_missing"))
with old.begin() as conn:
    conn.execute(text("CREATE INDEX uq_api_keys_key_instance_uuid ON api_keys (key_instance_uuid)"))
check("an index of that name that is NOT unique -> still 'unique_index_missing'",
      rvs.compute_key_identity(old, Session), "unique_index_missing")
with old.begin() as conn:
    conn.execute(text("DROP INDEX uq_api_keys_key_instance_uuid"))
    conn.execute(text("UPDATE api_keys SET key_instance_uuid = :u"), {"u": key_uuids[0]})
check("the added-columns inspection reports the missing index", "api_keys: unique index uq_api_keys_key_instance_uuid is missing"
      in rvs.inspect_added_columns(old))
outcome = rvs.bootstrap(old, Session)
check("bootstrap with that index missing is NOT ready (fail-closed)", outcome["ready"], False)

print("--- fail-closed, never fatal ---")
broken = new_engine()


@event.listens_for(broken, "before_cursor_execute")
def _fail(conn, cursor, statement, parameters, context, executemany):
    if statement.lstrip().upper().startswith("CREATE TABLE WALLET_LOTS"):
        raise RuntimeError("injected DDL failure")


raised, errors = run_startup(broken)
check("a DDL failure on one receipt-void table does not stop startup", raised, None)
check("...every project table and the provisioning schema are still there",
      (sorted(set(models.Base.metadata.tables) - set(inspect(broken).get_table_names())), ps.is_ready()), ([], True))
check("...receipt void is not ready, and says where it failed",
      (rvs.is_ready(), any(p.startswith("bootstrap failed during create") for p in rvs.status()["problems"])), (False, True))
try:
    rvs.assert_ready()
    asserted = None
except rvs.ReceiptVoidSchemaNotReady:
    asserted = "raised"
check("...assert_ready() raises", asserted, "raised")
check("...the failure is logged with a traceback",
      any("receipt void schema bootstrap failed" in r.getMessage() and r.exc_info for r in errors))

wrong = new_engine()
models.Base.metadata.create_all(wrong)
with wrong.begin() as conn:
    ddl = str(CreateTable(rv.wallet_lots).compile(dialect=wrong.dialect))
    edited = ddl.replace("CHECK (original_amount > 0)", "CHECK (1 = 1)")
    assert edited != ddl
    for table in rv.RECEIPT_VOID_TABLES:
        if table.name == "wallet_lots":
            conn.exec_driver_sql(edited)
            break
        conn.exec_driver_sql(str(CreateTable(table).compile(dialect=wrong.dialect)))
outcome = rvs.bootstrap(wrong, sessionmaker(bind=wrong))
check("a table with the right name but a gutted CHECK keeps receipt void not ready",
      (outcome["ready"], any("ck_walletlot_original_positive has a different expression" in p for p in outcome["problems"])),
      (False, True))
with wrong.connect() as conn:
    seeded = conn.execute(text("SELECT COUNT(*) FROM receipt_approval_runtime_state")).scalar()
check("...and nothing was seeded", seeded, 0)

print("--- MariaDB/MySQL: compiled DDL ---")
ddl = {t.name: str(CreateTable(t).compile(dialect=mysql.dialect())) for t in rv.RECEIPT_VOID_TABLES}
check("all 36 compile for MySQL", len(ddl), 36)
check("every named CHECK is in the DDL",
      [c.name for t in rv.RECEIPT_VOID_TABLES for c in t.constraints
       if c.__class__.__name__ == "CheckConstraint" and f"CONSTRAINT {c.name} CHECK" not in ddl[t.name]], [])
check("singletons and natural keys are not AUTO_INCREMENT",
      [n for n in ("receipt_approval_runtime_state", "wallet_runtime_state", "resource_locks", "auto_approval_subjects",
                   "payment_card_pool_states", "payment_card_pool_baselines") if "AUTO_INCREMENT" in ddl[n]], [])
check("timestamps are DATETIME(6)", "auto_granted_at DATETIME(6)" in ddl["receipt_approvals"])
check("the card foreign key is ON DELETE SET NULL, everything else RESTRICT",
      ("REFERENCES payment_cards (id) ON DELETE SET NULL" in ddl["payment_card_pool_events"],
       sorted({line.split("ON DELETE ")[1].rstrip(", ") for sql in ddl.values() for line in sql.splitlines() if "ON DELETE" in line})),
      (True, ["RESTRICT", "SET NULL"]))

mariadb_url = os.environ.get("MARIADB_TEST_URL", "").strip()
if mariadb_url:
    print("--- real MariaDB ---")
    maria = create_engine(mariadb_url)
    drop_all(maria)
    models.Base.metadata.create_all(maria)
    try:
        run_bootstrap_checks(maria, "mariadb")
        drop_all(maria)
        rvs.create_tables(maria)
        run_constraint_checks(maria, "mariadb")
    finally:
        drop_all(maria)
        models.Base.metadata.drop_all(maria)
        maria.dispose()
elif os.environ.get("CI"):
    check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
else:
    print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI)")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
