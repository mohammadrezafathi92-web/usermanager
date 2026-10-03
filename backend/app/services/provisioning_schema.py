"""Startup seeding + verification for the durable-provisioning schema
(Lifecycle/P6 batch L0 - docs/lifecycle-provisioning-design-2026-10-01.md,
sections 4 and 5).

Why this exists: `Base.metadata.create_all()` builds brand-new tables with
all of their constraints, but nothing ever proves it did. On an install
where a table of the same name was created some other way (an older build,
a hand-run script, a restore from a different version), create_all skips
it, and main.py's _auto_migrate_missing_columns only ADDS columns and
indexes - it never adds a CHECK or UNIQUE constraint, and it downgrades an
index that fails to build to a warning. So "the model declares it" is not
"the database enforces it". Every invariant of the durable-provisioning
design lives in those constraints, and code that relied on one that is
silently missing would corrupt money and provisioning state.

So at startup this module compares the live database against the models
with the inspector - tables, columns, unique constraints/indexes, check
constraints, plain indexes - and records the result. It is FAIL-CLOSED and
never fatal:

- it never raises out of ensure_provisioning_schema(): a broken provisioning
  schema must not take the panel, RADIUS or the bots down, because nothing
  that runs today uses these tables;
- but is_ready() stays False, and every future durable code path must call
  assert_ready() first - so a database that cannot be trusted keeps every
  operation type on its legacy path.

L0 scope: schema, the two fixed-row seeds, and this check. No gate, no
runner, no remote call, no enforcement of anything on existing flows.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Optional

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from .. import models_provisioning as mp

logger = logging.getLogger(__name__)

RUNTIME_STATE_ID = 1


class ProvisioningSchemaNotReady(RuntimeError):
    """Raised by assert_ready() - the durable-provisioning schema has not
    been verified in this process (or was verified and found wrong)."""


_status: dict = {"ready": False, "checked_at": None, "problems": ["schema has not been checked yet"]}


def status() -> dict:
    """Copy of the last check's outcome: {ready, checked_at, problems}."""
    return {"ready": _status["ready"], "checked_at": _status["checked_at"], "problems": list(_status["problems"])}


def is_ready() -> bool:
    return bool(_status["ready"])


def assert_ready() -> None:
    if not _status["ready"]:
        raise ProvisioningSchemaNotReady("; ".join(_status["problems"]) or "provisioning schema not ready")


def _expected_unique_sets(table) -> list[tuple[str, frozenset]]:
    out = []
    for constraint in table.constraints:
        if constraint.__class__.__name__ == "UniqueConstraint":
            out.append((constraint.name, frozenset(c.name for c in constraint.columns)))
    for index in table.indexes:
        if index.unique:
            out.append((index.name, frozenset(c.name for c in index.columns)))
    return out


def _expected_check_names(table) -> list[str]:
    return sorted(
        c.name for c in table.constraints
        if c.__class__.__name__ == "CheckConstraint" and c.name
    )


def inspect_schema(engine) -> list[str]:
    """Every difference between the provisioning models and the live
    database, as human-readable strings. Empty list == the database really
    carries the schema. Anything the dialect cannot report on is itself a
    problem - "could not verify" is never treated as "fine"."""
    problems: list[str] = []
    inspector = inspect(engine)
    try:
        live_tables = set(inspector.get_table_names())
    except Exception as exc:  # noqa: BLE001
        return [f"could not list tables: {exc}"]

    for table in mp.PROVISIONING_TABLES:
        name = table.name
        if name not in live_tables:
            problems.append(f"{name}: table is missing")
            continue

        try:
            live_columns = {c["name"]: c for c in inspector.get_columns(name)}
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: could not read columns: {exc}")
            continue
        for column in table.columns:
            live = live_columns.get(column.name)
            if live is None:
                problems.append(f"{name}.{column.name}: column is missing")
                continue
            # A primary-key column is NOT NULL by definition, but SQLite's
            # inspector reports nullable=True for it unless the DDL also
            # said NOT NULL - so only non-PK columns are compared.
            if not column.primary_key and bool(live.get("nullable", True)) != bool(column.nullable):
                problems.append(
                    f"{name}.{column.name}: nullable is {live.get('nullable')!r}, expected {column.nullable!r}"
                )

        try:
            pk_columns = set(inspector.get_pk_constraint(name).get("constrained_columns") or [])
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: could not read primary key: {exc}")
        else:
            expected_pk = {c.name for c in table.primary_key.columns}
            if pk_columns != expected_pk:
                problems.append(f"{name}: primary key is {sorted(pk_columns)}, expected {sorted(expected_pk)}")

        # Uniqueness can surface as a unique constraint (SQLite) or as a
        # unique index (MySQL/MariaDB) depending on the dialect - either is
        # real enforcement, so both are collected and matched by column set.
        try:
            live_unique = {
                frozenset(u.get("column_names") or [])
                for u in inspector.get_unique_constraints(name)
            }
            live_indexes = inspector.get_indexes(name)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: could not read unique constraints/indexes: {exc}")
        else:
            live_unique |= {
                frozenset(i.get("column_names") or []) for i in live_indexes if i.get("unique")
            }
            for unique_name, columns in _expected_unique_sets(table):
                if columns not in live_unique:
                    problems.append(f"{name}: unique {unique_name} on {sorted(columns)} is missing")
            live_index_columns = {tuple(i.get("column_names") or []) for i in live_indexes}
            for index in table.indexes:
                if index.unique:
                    continue
                if tuple(c.name for c in index.columns) not in live_index_columns:
                    problems.append(f"{name}: index {index.name} is missing")

        expected_checks = _expected_check_names(table)
        if expected_checks:
            try:
                live_checks = {c.get("name") for c in inspector.get_check_constraints(name)}
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{name}: could not read check constraints: {exc}")
            else:
                for check_name in expected_checks:
                    if check_name not in live_checks:
                        problems.append(f"{name}: check {check_name} is missing")

    return problems


def seed_fixed_rows(db) -> None:
    """The two tables that have a fixed set of rows from day one:

    - provisioning_runtime_state: the singleton, at its inert defaults
      (lock_backend='none', gate_mode='off', owner_state='none') with a
      random installation_uuid generated ONCE - an existing row is never
      rewritten, the uuid is the installation's identity.
    - provisioning_type_modes: one 'legacy' row per operation_type.

    Idempotent, and safe if two processes start at the same moment: the
    loser's INSERT hits the primary key, is rolled back, and the winner's
    row is the one that stays."""
    now = dt.datetime.utcnow()
    if db.get(mp.ProvisioningRuntimeState, RUNTIME_STATE_ID) is None:
        db.add(mp.ProvisioningRuntimeState(
            id=RUNTIME_STATE_ID,
            installation_uuid=str(uuid.uuid4()),
            gate_mode_changed_at=now,
            changed_at=now,
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()

    existing = {row.operation_type for row in db.query(mp.ProvisioningTypeMode.operation_type)}
    for operation_type in mp.OPERATION_TYPES:
        if operation_type in existing:
            continue
        db.add(mp.ProvisioningTypeMode(operation_type=operation_type, mode="legacy", changed_at=now))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()


def _seed_problems(db) -> list[str]:
    problems: list[str] = []
    row = db.get(mp.ProvisioningRuntimeState, RUNTIME_STATE_ID)
    if row is None:
        problems.append("provisioning_runtime_state: singleton row is missing")
    elif not row.installation_uuid:
        problems.append("provisioning_runtime_state: installation_uuid is empty")
    if db.query(mp.ProvisioningRuntimeState).count() > 1:
        problems.append("provisioning_runtime_state: more than one row")

    modes = {row.operation_type for row in db.query(mp.ProvisioningTypeMode.operation_type)}
    for operation_type in mp.OPERATION_TYPES:
        if operation_type not in modes:
            problems.append(f"provisioning_type_modes: row for {operation_type} is missing")
    for unknown in sorted(modes - set(mp.OPERATION_TYPES)):
        problems.append(f"provisioning_type_modes: unknown operation_type {unknown!r}")
    return problems


def ensure_provisioning_schema(engine, session_factory, now: Optional[dt.datetime] = None) -> dict:
    """Called once from main.on_startup, after create_all. Verifies the
    schema, seeds the fixed rows, verifies the seeds, and records the
    outcome for is_ready()/assert_ready(). Never raises."""
    problems: list[str] = []
    try:
        problems = inspect_schema(engine)
        if not problems:
            db = session_factory()
            try:
                seed_fixed_rows(db)
                problems = _seed_problems(db)
            finally:
                db.close()
    except Exception as exc:  # noqa: BLE001
        logger.exception("provisioning schema check failed")
        problems = problems + [f"schema check raised: {exc}"]

    _status["ready"] = not problems
    _status["checked_at"] = now or dt.datetime.utcnow()
    _status["problems"] = problems
    if problems:
        logger.error(
            "provisioning schema is NOT ready - every operation type stays on its legacy path "
            "(%d problem(s)): %s", len(problems), "; ".join(problems[:20]),
        )
    return status()
