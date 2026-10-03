"""Lifecycle/P6 batch L0 - the durable-provisioning schema must be
FAIL-CLOSED and NEVER FATAL.

Design: docs/lifecycle-provisioning-design-2026-10-01.md (v7.1, frozen),
section 4 (migration row) and the L0 row of section 16.

Run:  python3 backend/tests/test_provisioning_schema_guard.py

Three things are proven:

  A. STARTUP. main._create_schema() - the real sequence on_startup runs -
     keeps building the project's own tables and never raises, whatever
     happens to the provisioning tables: a DDL failure on one of them, a
     bootstrap that blows up, a table left behind in the wrong shape.
     Readiness stays False, nothing is seeded, the error is in the log.

  B. INSPECTOR. A schema that has the right table/column/constraint NAMES
     but the wrong substance is NOT reported ready: a wrong column type, a
     CHECK with the right name and a different expression, a missing or
     re-pointed foreign key, a wrong ON DELETE rule, a missing or wrong
     unique/index, a wrong default, nullability, a missing table.

  C. SEEDS. A non-canonical installation_uuid, a fixed row with the wrong
     value, an extra row - each keeps readiness False.

B and C run against real SQLite always and against real MariaDB whenever
MARIADB_TEST_URL is set; in CI its absence is a failure (see
tests/test_provisioning_schema_l0.py's docstring).
"""
from __future__ import annotations

import logging
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateIndex, CreateTable

from app import main, models
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
LEGACY_TABLE_NAMES = sorted(models.Base.metadata.tables)


def new_engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False})


def drop_provisioning_tables(engine):
    for table in reversed(mp.PROVISIONING_TABLES):
        table.drop(engine, checkfirst=True)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def run_startup_schema(engine):
    """Runs the REAL startup schema sequence (main._create_schema) against
    `engine`, exactly as on_startup does, and returns (raised, log records)."""
    capture = _Capture()
    root = logging.getLogger()
    root.addHandler(capture)
    saved = (main.engine, main.SessionLocal)
    main.engine, main.SessionLocal = engine, sessionmaker(bind=engine)
    raised = None
    try:
        main._create_schema()
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    finally:
        main.engine, main.SessionLocal = saved
        root.removeHandler(capture)
    return raised, capture.records


def legacy_tables_missing(engine) -> list[str]:
    return sorted(set(LEGACY_TABLE_NAMES) - set(inspect(engine).get_table_names()))


def row_count(engine, table_name):
    if table_name not in inspect(engine).get_table_names():
        return None
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar()


# ===========================================================================
print("--- A. startup: a healthy fresh install ---")
fresh = new_engine()
raised, records = run_startup_schema(fresh)
check("fresh install: _create_schema does not raise", raised, None)
check("fresh install: every pre-existing project table is created", legacy_tables_missing(fresh), [])
check("fresh install: the ten provisioning tables are created too",
      sorted(set(NEW_TABLE_NAMES) - set(inspect(fresh).get_table_names())), [])
check("fresh install: provisioning is ready", (ps.is_ready(), ps.status()["problems"]), (True, []))
check("fresh install: nothing was logged as an error", [r.getMessage() for r in records], [])

print("--- A. startup: DDL failure on ONE provisioning table ---")
for victim in ("provisioning_steps", "provisioning_operations", "provisioning_gate_stats"):
    broken = new_engine()

    @event.listens_for(broken, "before_cursor_execute")
    def _fail_ddl(conn, cursor, statement, parameters, context, executemany, _victim=victim):
        if statement.lstrip().upper().startswith(f"CREATE TABLE {_victim.upper()}"):
            raise RuntimeError(f"injected DDL failure on {_victim}")

    raised, records = run_startup_schema(broken)
    check(f"[{victim}] no exception leaves the startup schema sequence", raised, None)
    check(f"[{victim}] every pre-existing project table was still created", legacy_tables_missing(broken), [])
    check(f"[{victim}] readiness is False", ps.is_ready(), False)
    check(f"[{victim}] the recorded problem names the failed stage",
          any(p.startswith("bootstrap failed during create") for p in ps.status()["problems"]))
    try:
        ps.assert_ready()
        asserted = None
    except ps.ProvisioningSchemaNotReady:
        asserted = "raised"
    check(f"[{victim}] assert_ready() raises", asserted, "raised")
    check(f"[{victim}] no seed row was written (seeding never ran)",
          [(name, row_count(broken, name)) for name in ("provisioning_runtime_state", "provisioning_type_modes")
           if row_count(broken, name) not in (None, 0)],
          [])
    check(f"[{victim}] the provisioning error is in the log, with a traceback",
          any("provisioning schema bootstrap failed" in r.getMessage() and r.exc_info for r in records))
    check(f"[{victim}] ...and the 'NOT ready' summary is logged",
          any("provisioning schema is NOT ready" in r.getMessage() for r in records))

print("--- A. startup: bootstrap itself blowing up is contained by main's own guard ---")
exploding = new_engine()
real_bootstrap = ps.bootstrap


def _boom(*args, **kwargs):
    raise RuntimeError("bootstrap exploded before recording anything")


ps.bootstrap = _boom
try:
    raised, records = run_startup_schema(exploding)
finally:
    ps.bootstrap = real_bootstrap
check("no exception leaves the startup schema sequence", raised, None)
check("pre-existing project tables were still created", legacy_tables_missing(exploding), [])
check("readiness is False", ps.is_ready(), False)
check("main logged the failure with a traceback",
      any("provisioning schema bootstrap raised" in r.getMessage() and r.exc_info for r in records))

print("--- A. startup: auto-migration never 'repairs' a provisioning table ---")
stale = new_engine()
with stale.begin() as conn:
    # An older/foreign payment_reservations: no `version` column, no constraints.
    conn.execute(text(
        "CREATE TABLE payment_reservations (id INTEGER PRIMARY KEY, operation_id INTEGER NOT NULL, "
        "payer_kind VARCHAR(20) NOT NULL, generation VARCHAR(16) NOT NULL, user_id INTEGER, admin_id INTEGER, "
        "amount BIGINT NOT NULL, hold_ref CHAR(36) NOT NULL, state VARCHAR(10) NOT NULL, "
        "capture_ledger_entry_id INTEGER, reserved_at DATETIME NOT NULL, captured_at DATETIME, "
        "released_at DATETIME)"
    ))
raised, records = run_startup_schema(stale)
check("stale table: startup does not raise", raised, None)
check("stale table: pre-existing project tables created", legacy_tables_missing(stale), [])
check("stale table: auto-migration did NOT add the missing column to it",
      "version" in {c["name"] for c in inspect(stale).get_columns("payment_reservations")}, False)
check("stale table: it is reported, not patched",
      "payment_reservations.version: column is missing" in ps.status()["problems"])
check("stale table: readiness is False and nothing was seeded",
      (ps.is_ready(), row_count(stale, "provisioning_type_modes")), (False, 0))

print("--- A. startup: LP-67 - an existing database gains the tables, nothing existing changes ---")
old = new_engine()
models.Base.metadata.create_all(old)
before = {name: sorted(c["name"] for c in inspect(old).get_columns(name)) for name in LEGACY_TABLE_NAMES}
check("the old database has none of the new tables",
      sorted(set(NEW_TABLE_NAMES) & set(inspect(old).get_table_names())), [])
raised, records = run_startup_schema(old)
after = {name: sorted(c["name"] for c in inspect(old).get_columns(name)) for name in LEGACY_TABLE_NAMES}
check("upgrade: no exception, ready", (raised, ps.is_ready()), (None, True))
check("upgrade: no pre-existing table gained or lost a column", after, before)


# ===========================================================================
def build_schema(engine, edits=None, skip_tables=(), skip_indexes=(), index_edits=None):
    """Creates the ten tables from the models' own DDL for this dialect,
    with targeted textual edits - so every case differs from the real
    schema in exactly one way. An edit that changes nothing is a bug in the
    test, hence the assert."""
    drop_provisioning_tables(engine)
    edits = edits or {}
    index_edits = index_edits or {}
    with engine.begin() as conn:
        for table in mp.PROVISIONING_TABLES:
            if table.name in skip_tables:
                continue
            ddl = str(CreateTable(table).compile(dialect=engine.dialect))
            if table.name in edits:
                edited = edits[table.name](ddl)
                assert edited != ddl, f"edit for {table.name} changed nothing"
                ddl = edited
            conn.exec_driver_sql(ddl)
            for index in table.indexes:
                if index.name in skip_indexes:
                    continue
                index_ddl = str(CreateIndex(index).compile(dialect=engine.dialect))
                if index.name in index_edits:
                    index_ddl = index_edits[index.name](index_ddl)
                conn.exec_driver_sql(index_ddl)


def sub(pattern, replacement):
    return lambda ddl: re.sub(pattern, replacement, ddl, count=1)


def replace(old, new):
    return lambda ddl: ddl.replace(old, new, 1)


def chain(*edits):
    def apply(ddl):
        for edit in edits:
            ddl = edit(ddl)
        return ddl
    return apply


FK_CLAUSE = r",\s*FOREIGN KEY\(operation_id\) REFERENCES provisioning_operations \(id\) ON DELETE RESTRICT"

# (label, build_schema kwargs, fragment that must appear in a reported problem)
ADVERSARIAL = [
    ("wrong column type under the right name (amount BIGINT -> TEXT)",
     dict(edits={"payment_reservations": replace("amount BIGINT NOT NULL", "amount TEXT NOT NULL")}),
     "payment_reservations.amount: type is"),
    ("CHECK with the right name and a vacuous expression (1 = 1)",
     dict(edits={"payment_reservations": replace("CHECK (amount > 0)", "CHECK (1 = 1)")}),
     "payment_reservations: check ck_payres_amount_positive has a different expression"),
    ("the exact case found in review: TEXT amount AND a renamed-in-place CHECK (1 = 1)",
     dict(edits={"payment_reservations": chain(replace("amount BIGINT NOT NULL", "amount TEXT NOT NULL"),
                                               replace("CHECK (amount > 0)", "CHECK (1 = 1)"))}),
     "payment_reservations: check ck_payres_amount_positive has a different expression"),
    ("CHECK weakened by one operator (> becomes >=)",
     dict(edits={"payment_reservations": replace("CHECK (amount > 0)", "CHECK (amount >= 0)")}),
     "payment_reservations: check ck_payres_amount_positive has a different expression"),
    ("CHECK ... IN list missing one allowed value",
     dict(edits={"provisioning_steps": replace("'marzneshin', 'sui'))", "'marzneshin'))")}),
     "provisioning_steps: check ck_provstep_backend has a different expression"),
    ("CHECK ... IN list with one extra value",
     dict(edits={"provisioning_type_modes": replace("('legacy', 'durable')", "('legacy', 'durable', 'hybrid')")}),
     "provisioning_type_modes: check ck_provmode_mode has a different expression"),
    ("CHECK referring to a different column",
     dict(edits={"provisioning_operations": replace(
         "CHECK ((approval_uuid IS NULL) = (approval_key_bound IS NULL))",
         "CHECK ((approval_uuid IS NULL) = (actor_id IS NULL))")}),
     "provisioning_operations: check ck_provop_approval_bound_pair has a different expression"),
    ("CHECK dropped entirely",
     dict(edits={"payment_reservations": sub(r",\s*CONSTRAINT ck_payres_amount_positive CHECK \(amount > 0\)", "")}),
     "payment_reservations: check ck_payres_amount_positive is missing"),
    ("VARCHAR shorter than declared",
     dict(edits={"provisioning_operations": replace("business_key VARCHAR(160)", "business_key VARCHAR(100)")}),
     "provisioning_operations.business_key: type is"),
    ("CHAR turned into VARCHAR of the same length",
     dict(edits={"payment_reservations": replace("hold_ref CHAR(36)", "hold_ref VARCHAR(36)")}),
     "payment_reservations.hold_ref: type is"),
    ("NOT NULL dropped",
     dict(edits={"payment_reservations": replace("hold_ref CHAR(36) NOT NULL", "hold_ref CHAR(36)")}),
     "payment_reservations.hold_ref: nullable is"),
    ("server default changed ('legacy' -> 'durable')",
     dict(edits={"provisioning_type_modes": replace("DEFAULT 'legacy'", "DEFAULT 'durable'")}),
     "provisioning_type_modes.mode: server default is"),
    ("server default removed",
     dict(edits={"provisioning_runtime_state": replace(" DEFAULT 'off'", "")}),
     "provisioning_runtime_state.gate_mode: server default is"),
    ("foreign key removed",
     dict(edits={"provisioning_steps": sub(FK_CLAUSE, "")}),
     "provisioning_steps: foreign key ['operation_id'] -> provisioning_operations['id'] is missing"),
    ("ON DELETE RESTRICT turned into CASCADE",
     dict(edits={"payment_reservations": replace("ON DELETE RESTRICT", "ON DELETE CASCADE")}),
     "payment_reservations: foreign key ['operation_id'] is ON DELETE CASCADE"),
    ("ON DELETE RESTRICT turned into SET NULL",
     dict(edits={"one_time_package_claims": chain(sub(r"operation_id (INTEGER|BIGINT) NOT NULL", r"operation_id \1"),
                                                  replace("ON DELETE RESTRICT", "ON DELETE SET NULL"))}),
     "one_time_package_claims: foreign key ['operation_id'] is ON DELETE SET NULL"),
    ("UNIQUE removed",
     dict(edits={"payment_reservations": sub(r",\s*CONSTRAINT uq_payres_hold_ref UNIQUE \(hold_ref\)", "")}),
     "payment_reservations: unique uq_payres_hold_ref on ['hold_ref'] is missing"),
    ("UNIQUE kept by name but on the wrong columns",
     dict(edits={"payment_reservations": replace("UNIQUE (operation_id, payer_kind)", "UNIQUE (operation_id, state)")}),
     "payment_reservations: unique uq_payres_operation_payer on ['operation_id', 'payer_kind'] is missing"),
    ("NULL-exempt approval UNIQUE removed",
     dict(edits={"provisioning_operations": sub(
         r",\s*CONSTRAINT uq_provop_approval_version UNIQUE \(approval_uuid, approval_execution_version\)", "")}),
     "provisioning_operations: unique uq_provop_approval_version"),
    ("index removed",
     dict(skip_indexes=("ix_provstep_state_retry",)),
     "provisioning_steps: index ix_provstep_state_retry on ['state', 'next_retry_at'] is missing"),
    ("index kept by name but on the wrong columns",
     dict(index_edits={"ix_provstep_state_retry": replace("(state, next_retry_at)", "(state)")}),
     "provisioning_steps: index ix_provstep_state_retry on ['state', 'next_retry_at'] is missing"),
    ("an unexpected extra column",
     dict(edits={"provisioning_gate_stats": replace("node_id INTEGER NOT NULL, ",
                                                    "node_id INTEGER NOT NULL, \n\tsurprise INTEGER, ")}),
     "provisioning_gate_stats.surprise: unexpected column"),
    ("primary key on the wrong column set",
     dict(edits={"provisioning_node_contracts": replace("PRIMARY KEY (node_id, backend)", "PRIMARY KEY (node_id)")}),
     "provisioning_node_contracts: primary key is ['node_id']"),
]

# Only MySQL/MariaDB can express (and report) these two.
ADVERSARIAL_MYSQL_ONLY = [
    ("DATETIME without its (6) fractional seconds",
     dict(edits={"provisioning_operations": replace("forward_deadline DATETIME(6)", "forward_deadline DATETIME")}),
     "provisioning_operations.forward_deadline: type is"),
    ("AUTO_INCREMENT on a key that must not have it",
     dict(edits={"provisioning_node_reconciliations": replace("node_id INTEGER NOT NULL",
                                                              "node_id INTEGER NOT NULL AUTO_INCREMENT")}),
     "provisioning_node_reconciliations.node_id: autoincrement is True"),
    ("surrogate id without AUTO_INCREMENT",
     dict(edits={"provisioning_ownership_events": replace("id BIGINT NOT NULL AUTO_INCREMENT", "id BIGINT NOT NULL")}),
     "provisioning_ownership_events.id: autoincrement is False"),
]


def run_inspector_checks(engine, tag):
    Session = sessionmaker(bind=engine)

    print(f"--- [{tag}] B. inspector: the real schema ---")
    build_schema(engine)
    check(f"[{tag}] the unedited schema inspects clean", ps.inspect_schema(engine), [])
    check(f"[{tag}] ...and bootstraps ready", ps.bootstrap(engine, Session)["ready"], True)

    print(f"--- [{tag}] B. inspector: right names, wrong substance ---")
    cases = list(ADVERSARIAL)
    if engine.dialect.name != "sqlite":
        cases += ADVERSARIAL_MYSQL_ONLY
    for label, kwargs, fragment in cases:
        build_schema(engine, **kwargs)
        problems = ps.inspect_schema(engine)
        if not any(fragment in p for p in problems):
            print(f"        problems reported: {problems!r}")
        check(f"[{tag}] {label}: reported", any(fragment in p for p in problems))
        outcome = ps.bootstrap(engine, Session)
        check(f"[{tag}] {label}: bootstrap is not ready", (outcome["ready"], ps.is_ready()), (False, False))
        check(f"[{tag}] {label}: nothing seeded", row_count(engine, "provisioning_type_modes"), 0)

    print(f"--- [{tag}] B. inspector: a missing table is re-created only if create_all can; a wrong one never is ---")
    build_schema(engine, skip_tables=("provisioning_gate_stats",))
    check(f"[{tag}] missing table: reported by inspect_schema",
          "provisioning_gate_stats: table is missing" in ps.inspect_schema(engine))
    check(f"[{tag}] missing table: bootstrap creates it and is then ready",
          ps.bootstrap(engine, Session)["ready"], True)

    print(f"--- [{tag}] C. seeds ---")

    def reseed(prepare):
        build_schema(engine)
        db = Session()
        try:
            ps.seed_fixed_rows(db)
            prepare(db)
            db.commit()
        finally:
            db.close()
        return ps.bootstrap(engine, Session)

    def singleton(db):
        return db.get(mp.ProvisioningRuntimeState, 1)

    def set_singleton(**values):
        def prepare(db):
            for key, value in values.items():
                setattr(singleton(db), key, value)
        return prepare

    def set_mode(operation_type, mode):
        def prepare(db):
            db.get(mp.ProvisioningTypeMode, operation_type).mode = mode
        return prepare

    def add_mode_row(db):
        import datetime as dt
        db.add(mp.ProvisioningTypeMode(operation_type="not_an_operation", mode="legacy", changed_at=dt.datetime.utcnow()))

    def delete_mode_then_block(db):
        db.delete(db.get(mp.ProvisioningTypeMode, "purchase"))

    check(f"[{tag}] untouched seeds: ready", reseed(lambda db: None)["ready"], True)
    upper = str(uuid.uuid4()).upper()
    seed_cases = [
        ("installation_uuid that is not a UUID", set_singleton(installation_uuid="not-a-uuid"),
         "installation_uuid is not a canonical UUID"),
        ("installation_uuid in non-canonical (upper-case) form", set_singleton(installation_uuid=upper),
         "installation_uuid is not a canonical UUID"),
        ("installation_uuid without dashes", set_singleton(installation_uuid=uuid.uuid4().hex),
         "installation_uuid is not a canonical UUID"),
        ("singleton with a non-L0 ownership_epoch", set_singleton(ownership_epoch=7),
         "provisioning_runtime_state.ownership_epoch: is 7"),
        ("singleton with a non-L0 gate_mode_epoch", set_singleton(gate_mode_epoch=3),
         "provisioning_runtime_state.gate_mode_epoch: is 3"),
        ("singleton owner_state not 'none'", set_singleton(owner_state="released"),
         "provisioning_runtime_state.owner_state: is 'released'"),
        ("a type mode that is not 'legacy'", set_mode("purchase", "durable"),
         "provisioning_type_modes.purchase: mode is 'durable'"),
        ("an extra, unknown type-mode row", add_mode_row,
         "provisioning_type_modes: unknown operation_type 'not_an_operation'"),
    ]
    for label, prepare, fragment in seed_cases:
        outcome = reseed(prepare)
        check(f"[{tag}] {label}: not ready", (outcome["ready"], ps.is_ready()), (False, False))
        if not any(fragment in p for p in outcome["problems"]):
            print(f"        problems reported: {outcome['problems']!r}")
        check(f"[{tag}] {label}: reported", any(fragment in p for p in outcome["problems"]))

    outcome = reseed(delete_mode_then_block)
    check(f"[{tag}] a missing type-mode row is simply re-seeded (the row set is fixed)", outcome["ready"], True)


print("--- unit: CHECK normalization against how MySQL/MariaDB re-print expressions ---")
written = "(approval_key_bound IS NOT NULL AND approval_key_bound = 1) = (execution_key_instance_uuid IS NOT NULL)"
check("MariaDB-style reprint (backticks, lowercase keywords) is the same expression",
      ps.normalize_check("(`approval_key_bound` is not null and `approval_key_bound` = 1) = "
                         "(`execution_key_instance_uuid` is not null)", "mariadb"),
      ps.normalize_check(written, "mariadb"))
check("MySQL-8-style reprint (extra parentheses everywhere) is the same expression",
      ps.normalize_check("((((`approval_key_bound` is not null) and (`approval_key_bound` = 1))) = "
                         "(`execution_key_instance_uuid` is not null))", "mysql"),
      ps.normalize_check(written, "mysql"))
check("charset introducers before literals are ignored on MySQL",
      ps.normalize_check("(`mode` in (_utf8mb4'legacy',_utf8mb4'durable'))", "mysql"),
      ps.normalize_check("mode IN ('legacy', 'durable')", "mysql"))
check("LENGTH reported as octet_length is the same function",
      ps.normalize_check("octet_length(`claim_key`) = 64", "mariadb"),
      ps.normalize_check("LENGTH(claim_key) = 64", "mariadb"))
check("a literal containing '+' and '_' survives verbatim",
      "'flock+get_lock'" in ps.normalize_check("lock_backend IN ('none', 'flock', 'flock+get_lock')", "mysql"))
check("string literals are case-sensitive (not lowercased)",
      ps.normalize_check("mode IN ('Legacy', 'durable')", "mysql") ==
      ps.normalize_check("mode IN ('legacy', 'durable')", "mysql"), False)
check("a different column is a different expression",
      ps.normalize_check("`amount` > 0", "mariadb") == ps.normalize_check("`user_id` > 0", "mariadb"), False)
check("a different operator is a different expression",
      ps.normalize_check("`amount` >= 0", "mariadb") == ps.normalize_check("amount > 0", "mariadb"), False)
check("on SQLite parentheses are part of the comparison",
      ps.normalize_check("a = 1 OR (b = 2 AND c = 3)", "sqlite") ==
      ps.normalize_check("(a = 1 OR b = 2) AND c = 3", "sqlite"), False)
check("an expression that cannot be tokenized is never 'equal'", ps.normalize_check("amount > 0 ; ¤", "sqlite"), None)

print("--- unit: type normalization ---")
from sqlalchemy.dialects import mysql as _my  # noqa: E402

check("MySQL TINYINT(1) is the Boolean column", ps.normalize_type(_my.TINYINT(1), "mysql"), ("bool",))
check("MySQL DATETIME keeps its precision", ps.normalize_type(_my.DATETIME(fsp=6), "mysql"), ("datetime", 6))
check("MySQL DATETIME without precision differs", ps.normalize_type(_my.DATETIME(), "mysql"), ("datetime", 0))
check("LONGTEXT is not TEXT", ps.normalize_type(_my.LONGTEXT(), "mysql"), ("unknown", "LONGTEXT"))
check("VARCHAR length is compared", ps.normalize_type(_my.VARCHAR(100), "mysql"), ("varchar", 100))

print("--- unit: what cannot be verified is a problem, never a pass ---")


class _Dialect:
    name = "postgresql"


class _UnsupportedEngine:
    dialect = _Dialect()


check("an unsupported dialect is reported, not waved through",
      ps.inspect_schema(_UnsupportedEngine()),
      ["dialect 'postgresql' is not supported by the provisioning schema check"])

healthy = new_engine()
ps.create_tables(healthy)
real_inspect = ps.inspect


class _NoChecks:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_check_constraints(self, table_name, **kw):
        raise NotImplementedError("this dialect cannot reflect CHECK constraints")


ps.inspect = lambda engine: _NoChecks(real_inspect(engine))
try:
    problems = ps.inspect_schema(healthy)
    outcome = ps.bootstrap(healthy, sessionmaker(bind=healthy))
finally:
    ps.inspect = real_inspect
check("an inspector that cannot read CHECKs makes every checked table a problem",
      sum(1 for p in problems if "could not read check constraints" in p), 9)
check("...and bootstrap is not ready", outcome["ready"], False)
check("...with nothing seeded", row_count(healthy, "provisioning_type_modes"), 0)

run_inspector_checks(new_engine(), "sqlite")

mariadb_url = os.environ.get("MARIADB_TEST_URL", "").strip()
if mariadb_url:
    maria = create_engine(mariadb_url)
    try:
        run_inspector_checks(maria, "mariadb")
    finally:
        drop_provisioning_tables(maria)
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
