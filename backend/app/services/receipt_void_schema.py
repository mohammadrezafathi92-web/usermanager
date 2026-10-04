"""Creation, verification and seeding of the Receipt Void schema - rollout
phase P0 of docs/receipt-void-design-2026-10-02.md (v16.1, frozen).

P0 is additive and inert:

- the 36 new tables of models_receipt_void.py (own MetaData, created here);
- ten nullable columns and two explicit unique indexes on three existing
  tables (declared in models.py, added by main.py's additive
  auto-migration);
- the two singleton rows, at values under which nothing is registered,
  correlated, rate-limited or voided;
- the API-key identity readiness computation of design 5.5 (backfill of
  ApiKey.key_instance_uuid + the unique index really being there).

Nothing in the application reads any of this yet. Same contract as
services/provisioning_schema.py, whose inspector it reuses: never fatal to
startup, and fail-closed - is_ready() is True only after create, inspect,
seed and validate all passed. Every later Receipt Void phase must call
assert_ready() before it does anything.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from .. import models
from .. import models_receipt_void as rv
from . import provisioning_schema

logger = logging.getLogger(__name__)

SINGLETON_ID = 1
API_KEY_UUID_INDEX = "uq_api_keys_key_instance_uuid"
LEDGER_REVERSAL_INDEX = "uq_ledger_entries_reversal_of_id"

# Columns P0 adds to existing tables: {table: [column, ...]}. Their presence
# is CHECKED here because the auto-migration that adds them only warns when
# an ALTER fails.
ADDED_COLUMNS = {
    "ledger_entries": ["approval_uuid", "amount_source", "voided_at", "void_operation_id", "reversal_of_id"],
    "discount_code_redemptions": ["approval_uuid", "voided_at", "void_operation_id"],
    "api_keys": ["key_instance_uuid", "approval_protocol_version"],
}
ADDED_UNIQUE_INDEXES = {"api_keys": API_KEY_UUID_INDEX, "ledger_entries": LEDGER_REVERSAL_INDEX}

# What the two singletons must look like while P0 is the only phase
# deployed: no code exists yet that may change these, so anything else is a
# corrupted or foreign row. The phase that adds a writer removes its field.
APPROVAL_RUNTIME_P0 = {
    "registration_mode": "off", "auto_rate_limit_mode": "off", "loyalty_timing_mode": "legacy_activation",
    "completeness_generation": 0, "activated_at": None,
}
WALLET_RUNTIME_P0 = {"phase": "normal", "epoch": 0, "external_fencing": "none"}


class ReceiptVoidSchemaNotReady(RuntimeError):
    pass


_status: dict = {"ready": False, "checked_at": None, "problems": ["schema has not been checked yet"],
                 "key_identity_ready": False, "key_identity_failure": "not_checked"}


def status() -> dict:
    return {**_status, "problems": list(_status["problems"])}


def is_ready() -> bool:
    return bool(_status["ready"])


def assert_ready() -> None:
    if not _status["ready"]:
        raise ReceiptVoidSchemaNotReady("; ".join(_status["problems"]) or "receipt void schema not ready")


def _record(problems: list[str]) -> dict:
    _status["ready"] = not problems
    _status["checked_at"] = dt.datetime.utcnow()
    _status["problems"] = list(problems)
    return status()


def mark_not_ready(reason: str) -> dict:
    return _record([reason])


def _widen_card_event_kind(engine) -> None:
    """payment_card_pool_events.event_kind was first created as VARCHAR(28),
    one short of 'payment_recorded_uncorrelated'. Nothing could be written
    to the table in that shape, so: MariaDB widens the column in place,
    SQLite (which cannot alter a column) drops the still-empty table and
    lets create_all build it again."""
    name = rv.payment_card_pool_events.name
    inspector = inspect(engine)
    if name not in inspector.get_table_names():
        return
    column = next((c for c in inspector.get_columns(name) if c["name"] == "event_kind"), None)
    if column is None or getattr(column["type"], "length", None) != 28:
        return
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            if conn.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar():
                return
            conn.execute(text(f"DROP TABLE {name}"))
        else:
            conn.execute(text(f"ALTER TABLE {name} MODIFY event_kind VARCHAR(32) NOT NULL"))
    logger.info("receipt void schema: widened %s.event_kind to 32", name)


def create_tables(engine) -> None:
    _widen_card_event_kind(engine)
    rv.RECEIPT_VOID_METADATA.create_all(bind=engine, tables=list(rv.RECEIPT_VOID_TABLES))


def inspect_added_columns(engine) -> list[str]:
    """The additive part on existing tables: every column is there, and the
    two unique indexes exist by name and ARE unique."""
    problems: list[str] = []
    inspector = inspect(engine)
    for table, columns in ADDED_COLUMNS.items():
        try:
            live = {c["name"] for c in inspector.get_columns(table)}
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{table}: could not read columns: {exc}")
            continue
        for column in columns:
            if column not in live:
                problems.append(f"{table}.{column}: column is missing")
    for table, index_name in ADDED_UNIQUE_INDEXES.items():
        try:
            match = [i for i in inspector.get_indexes(table) if i.get("name") == index_name]
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{table}: could not read indexes: {exc}")
            continue
        if not match:
            problems.append(f"{table}: unique index {index_name} is missing")
        elif not match[0].get("unique"):
            problems.append(f"{table}: index {index_name} exists but is not unique")
    return problems


def seed_singletons(db) -> None:
    """Both singleton rows, in one transaction, never rewriting an existing
    row. key_identity starts as NOT ready with an explicit reason - the
    readiness computation is what may set it to ready."""
    now = dt.datetime.utcnow()
    if db.execute(select(rv.receipt_approval_runtime_state.c.id)).first() is None:
        db.execute(rv.receipt_approval_runtime_state.insert().values(
            id=SINGLETON_ID, key_identity_ready=False, key_identity_failure="not_checked",
            key_identity_failed_at=now, updated_at=now,
        ))
    if db.execute(select(rv.wallet_runtime_state.c.id)).first() is None:
        db.execute(rv.wallet_runtime_state.insert().values(id=SINGLETON_ID, updated_at=now))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


def validate_seed(db) -> list[str]:
    problems: list[str] = []
    for table, expected in ((rv.receipt_approval_runtime_state, APPROVAL_RUNTIME_P0),
                            (rv.wallet_runtime_state, WALLET_RUNTIME_P0)):
        rows = db.execute(select(table)).mappings().all()
        if len(rows) != 1 or rows[0]["id"] != SINGLETON_ID:
            problems.append(f"{table.name}: expected exactly the singleton id={SINGLETON_ID}, "
                            f"found ids {sorted(r['id'] for r in rows)}")
            continue
        for column, value in expected.items():
            if rows[0][column] != value:
                problems.append(f"{table.name}.{column}: is {rows[0][column]!r}, expected {value!r}")
    return problems


def compute_key_identity(engine, session_factory) -> str | None:
    """Design 5.5, "computing readiness". Returns the failure code, or None
    when every API key has its own unique instance uuid AND the database
    enforces that uniqueness.

    Step A (read + backfill): the unique index exists; every NULL uuid gets
    a fresh one; no NULL and no duplicate remains.
    Step B (its own transaction): the outcome is written to the singleton,
    whatever it is - a failure must be durable too."""
    failure: str | None = None
    db = session_factory()
    try:
        try:
            indexes = [i for i in inspect(engine).get_indexes("api_keys") if i.get("name") == API_KEY_UUID_INDEX]
            if not indexes or not indexes[0].get("unique"):
                failure = "unique_index_missing"
            else:
                for key in db.query(models.ApiKey).filter(models.ApiKey.key_instance_uuid.is_(None)).all():
                    key.key_instance_uuid = str(uuid.uuid4())
                db.commit()
                if db.query(models.ApiKey).filter(models.ApiKey.key_instance_uuid.is_(None)).count():
                    failure = "null_uuid_remaining"
                elif db.query(models.ApiKey.key_instance_uuid).group_by(models.ApiKey.key_instance_uuid).having(
                        func.count() > 1).first() is not None:
                    failure = "duplicate_uuid"
        except Exception:  # noqa: BLE001
            logger.exception("receipt void: key identity check failed")
            db.rollback()
            failure = "check_error"

        now = dt.datetime.utcnow()
        table = rv.receipt_approval_runtime_state
        db.execute(update(table).where(table.c.id == SINGLETON_ID).values(
            key_identity_ready=failure is None, key_identity_failure=failure,
            key_identity_failed_at=now if failure else None, key_identity_checked_at=now,
            updated_at=now, version=table.c.version + 1,
        ))
        db.commit()
    finally:
        db.close()
    _status["key_identity_ready"], _status["key_identity_failure"] = failure is None, failure
    return failure


def bootstrap(engine, session_factory) -> dict:
    """create -> inspect (new tables + additive columns/indexes) -> seed ->
    validate -> key identity. Never raises. Each stage runs only if the one
    before left no problem."""
    problems: list[str] = []
    stage = "create"
    try:
        create_tables(engine)
        stage = "inspect"
        problems = provisioning_schema.inspect_schema(engine, rv.RECEIPT_VOID_TABLES) + inspect_added_columns(engine)
        if not problems:
            stage = "seed"
            db = session_factory()
            try:
                seed_singletons(db)
                stage = "validate"
                problems = validate_seed(db)
            finally:
                db.close()
        if not problems:
            stage = "key_identity"
            # A key-identity failure is NOT a schema problem: it is its own
            # durable flag, and only blocks the phases that need it.
            compute_key_identity(engine, session_factory)
    except Exception as exc:  # noqa: BLE001
        logger.exception("receipt void schema bootstrap failed during %s", stage)
        problems = problems + [f"bootstrap failed during {stage}: {exc.__class__.__name__}: {exc}"]

    outcome = _record(problems)
    if problems:
        logger.error("receipt void schema is NOT ready (%d problem(s)): %s", len(problems), "; ".join(problems[:20]))
    return outcome
