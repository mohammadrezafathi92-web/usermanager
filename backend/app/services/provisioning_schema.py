"""Creation, seeding and verification of the durable-provisioning schema
(Lifecycle/P6 batch L0 - docs/lifecycle-provisioning-design-2026-10-01.md,
sections 4 and 5).

The ten provisioning tables live on their own MetaData (see
models_provisioning.py), so main.py's unprotected create_all and its
auto-migration never see them. This module is the ONLY thing that creates
them, and bootstrap() is the single entry point, called once at startup.

Why verification exists: `create_all()` skips a table whose name already
exists, whatever its shape. A table left behind by an older build, a
hand-run script or a restore from another version would be accepted
silently - and every invariant of the durable design lives in its column
types, CHECKs, UNIQUEs and foreign keys. So after creating, bootstrap()
compares the live database against the models: column types (normalized
per dialect), nullability, server defaults, primary keys, auto-increment,
unique constraints (the exact set - an extra one is as wrong as a missing
one), required indexes, foreign keys with their ON DELETE rule, and CHECK
constraints (the exact set of names, each compared by its parsed
expression). Then it seeds the two fixed-row
tables and validates those rows.

Fail-closed, never fatal:

- nothing here ever raises out of bootstrap(): a broken provisioning schema
  must not take the panel, RADIUS or the bots down, because nothing that
  runs today uses these tables;
- any failure - DDL, inspection, an inspector capability the dialect lacks,
  a seed problem - leaves is_ready() False, and every future durable code
  path must call assert_ready() first, so an untrusted database keeps every
  operation type on its legacy path;
- rows are only seeded into a schema that verified, in one transaction.

L0 scope: schema, seeds, this check. No gate, no runner, no remote call, no
enforcement of anything on existing flows.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import uuid

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from .. import models_provisioning as mp

logger = logging.getLogger(__name__)

RUNTIME_STATE_ID = 1
SUPPORTED_DIALECTS = ("sqlite", "mysql", "mariadb")

# What the singleton must look like while L0 is the only batch deployed:
# no code exists yet that may change any of these, so anything else is a
# corrupted or foreign row. The batch that adds the writers of a field
# removes it from this map.
RUNTIME_STATE_L0_VALUES = {
    "lock_backend": "none",
    "gate_mode": "off",
    "gate_mode_epoch": 0,
    "owner_state": "none",
    "owner_host_id": None,
    "owner_boot_id": None,
    "ownership_epoch": 0,
    "owner_claimed_at": None,
    "owner_heartbeat_at": None,
    "lock_verified_at": None,
}


class ProvisioningSchemaNotReady(RuntimeError):
    """Raised by assert_ready() - the durable-provisioning schema has not
    been verified in this process (or was verified and found wrong)."""


_status: dict = {"ready": False, "checked_at": None, "problems": ["schema has not been checked yet"]}


def status() -> dict:
    """Copy of the last bootstrap's outcome: {ready, checked_at, problems}."""
    return {"ready": _status["ready"], "checked_at": _status["checked_at"], "problems": list(_status["problems"])}


def is_ready() -> bool:
    return bool(_status["ready"])


def assert_ready() -> None:
    if not _status["ready"]:
        raise ProvisioningSchemaNotReady("; ".join(_status["problems"]) or "provisioning schema not ready")


def _record(problems: list[str]) -> dict:
    _status["ready"] = not problems
    _status["checked_at"] = dt.datetime.utcnow()
    _status["problems"] = list(problems)
    return status()


# --------------------------------------------------------------- normalizing
def _is_mysql(dialect_name: str) -> bool:
    return dialect_name in ("mysql", "mariadb")


def normalize_type(sa_type, dialect_name: str) -> tuple:
    """Dialect-independent description of a column type, for comparing a
    model column with what the inspector reports.

    Deliberate equivalences (and nothing wider than these):
    - SQLite: the surrogate-id variant is INTEGER on both sides. DATETIME
      carries no precision there.
    - MySQL/MariaDB: BOOLEAN is stored and reported as TINYINT; DATETIME is
      compared WITH its fractional-seconds precision (the design needs 6).
    An unrecognized type normalizes to ('unknown', ...) and therefore never
    equals an expected type."""
    name = sa_type.__class__.__name__.upper()
    if name in ("BOOLEAN", "BOOL", "TINYINT"):
        return ("bool",)
    if name in ("BIGINT", "BIGINTEGER"):
        return ("bigint",)
    if name in ("INTEGER", "INT"):
        return ("int",)
    if name == "DATETIME":
        if _is_mysql(dialect_name):
            return ("datetime", getattr(sa_type, "fsp", None) or 0)
        return ("datetime",)
    if name == "TEXT":
        return ("text",)
    if name in ("VARCHAR", "STRING"):
        return ("varchar", getattr(sa_type, "length", None))
    if name == "CHAR":
        return ("char", getattr(sa_type, "length", None))
    if name in ("NUMERIC", "DECIMAL"):
        return ("numeric", getattr(sa_type, "precision", None), getattr(sa_type, "scale", None))
    return ("unknown", name)


def normalize_default(value) -> str | None:
    """A server default as a bare literal: None for "no default" (also how
    MariaDB's explicit DEFAULT NULL reads), otherwise the text with one
    layer of quoting/parentheses removed."""
    if value is None:
        return None
    text_value = getattr(value, "arg", value)
    text_value = str(getattr(text_value, "text", text_value)).strip()
    while text_value.startswith("(") and text_value.endswith(")"):
        text_value = text_value[1:-1].strip()
    if text_value.upper() == "NULL":
        return None
    if len(text_value) >= 2 and text_value[0] == text_value[-1] == "'":
        text_value = text_value[1:-1]
    return text_value


_TOKEN = re.compile(
    r"\s*("
    r"'(?:[^'\\]|''|\\.)*'"          # string literal
    r"|[A-Za-z_][A-Za-z0-9_$]*"      # identifier / keyword / function
    r"|[0-9]+(?:\.[0-9]+)?"          # number
    r"|<>|!=|<=|>=|=|<|>|\(|\)|,|\+|-"
    r")"
)
# MariaDB re-prints LENGTH() as octet_length() - the same function, both
# count BYTES. CHAR_LENGTH counts CHARACTERS and is deliberately NOT here:
# for non-ASCII text the two differ, so a CHECK rewritten from LENGTH to
# CHAR_LENGTH accepts values the original rejects.
_FUNCTION_SYNONYMS = {"octet_length": "length"}
_COMPARISONS = ("=", "<>", "<", ">", "<=", ">=")
_KEYWORDS = ("or", "and", "not", "is", "null", "in")


class _CheckSyntaxError(ValueError):
    pass


def _tokenize_check(sqltext: str) -> list[tuple[str, str]]:
    """(kind, value) tokens of a CHECK expression. Purely cosmetic
    differences are removed HERE and nowhere else: identifier quoting
    (` or "), whitespace, keyword/identifier case, != for <>, the spelling
    of LENGTH, and a _charset introducer in front of a string literal.
    Parentheses are tokens - grouping is the parser's business."""
    source = str(sqltext).replace("`", "").replace('"', "")
    raw: list[str] = []
    position = 0
    while position < len(source):
        if source[position:].strip() == "":
            break
        match = _TOKEN.match(source, position)
        if not match:
            raise _CheckSyntaxError(f"cannot tokenize at {source[position:position + 20]!r}")
        raw.append(match.group(1))
        position = match.end()

    tokens: list[tuple[str, str]] = []
    for index, token in enumerate(raw):
        if token.startswith("'"):
            tokens.append(("str", token))
        elif token[0].isdigit():
            tokens.append(("num", token))
        elif token[0].isalpha() or token[0] == "_":
            lowered = token.lower()
            # _utf8mb4'literal' (MySQL 8) - an introducer, not an operand.
            if lowered.startswith("_") and index + 1 < len(raw) and raw[index + 1].startswith("'"):
                continue
            if lowered in _KEYWORDS:
                tokens.append(("kw", lowered))
            else:
                tokens.append(("name", _FUNCTION_SYNONYMS.get(lowered, lowered)))
        else:
            tokens.append(("op", "<>" if token == "!=" else token))
    return tokens


class _CheckParser:
    """Recursive-descent parser for the small boolean language the CHECK
    constraints of this schema are written in, with SQL's real precedence:

        or_expr   := and_expr ( OR and_expr )*
        and_expr  := not_expr ( AND not_expr )*
        not_expr  := NOT not_expr | predicate
        predicate := sum ( <cmp> sum | IS [NOT] NULL | [NOT] IN ( operand, ... ) )*
        sum       := operand ( (+|-) operand )*
        operand   := literal | NULL | name | name ( args ) | ( or_expr )

    The result is a tree, so a pair of parentheses only disappears when it
    does not change that tree: `(a) AND ((b))` is `a AND b`, but
    `a OR (b AND c)` and `(a OR b) AND c` are different trees. A chain of
    the same operator is flattened into one n-ary node, because
    `(a AND b) AND c` and `a AND (b AND c)` are the same condition - that is
    how MySQL 8 re-prints a three-way AND.

    Anything outside this grammar raises - the caller treats that as a
    mismatch, never as a match."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.position = 0

    def _peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else (None, None)

    def _take(self, kind=None, value=None):
        token = self._peek()
        if token[0] is None or (kind and token[0] != kind) or (value and token[1] != value):
            raise _CheckSyntaxError(f"expected {value or kind}, found {token[1]!r}")
        self.position += 1
        return token

    def _at(self, kind, value):
        return self._peek() == (kind, value)

    def parse(self):
        tree = self._or()
        if self.position != len(self.tokens):
            raise _CheckSyntaxError(f"unexpected {self._peek()[1]!r}")
        return tree

    def _nary(self, keyword, operand):
        items = [operand()]
        while self._at("kw", keyword):
            self._take()
            items.append(operand())
        if len(items) == 1:
            return items[0]
        flat = []
        for item in items:
            flat.extend(item[1] if item[0] == keyword else [item])
        return (keyword, tuple(flat))

    def _or(self):
        return self._nary("or", self._and)

    def _and(self):
        return self._nary("and", self._not)

    def _not(self):
        if self._at("kw", "not"):
            self._take()
            inner = self._not()
            if inner[0] == "in":
                return ("not_in",) + inner[1:]
            if inner[0] == "cmp" and inner[1] == "=" and inner[2][0] == "name" and inner[3][0] in ("str", "num"):
                return ("cmp", "<>") + inner[2:]
            if inner[0] == "is_null":
                return ("is_not_null",) + inner[1:]
            return ("not", inner)
        return self._predicate()

    def _sum(self):
        left = self._operand()
        while self._peek() in (("op", "+"), ("op", "-")):
            operator = self._take()[1]
            left = ("arith", operator, left, self._operand())
        return left

    def _predicate(self):
        left = self._sum()
        while True:
            kind, value = self._peek()
            if kind == "op" and value in _COMPARISONS:
                self._take()
                left = ("cmp", value, left, self._sum())
            elif (kind, value) == ("kw", "is"):
                self._take()
                negated = self._at("kw", "not")
                if negated:
                    self._take()
                self._take("kw", "null")
                left = ("is_not_null" if negated else "is_null", left)
            elif (kind, value) == ("kw", "in") or (
                (kind, value) == ("kw", "not")
                and self.position + 1 < len(self.tokens)
                and self.tokens[self.position + 1] == ("kw", "in")
            ):
                negated = value == "not"
                if negated:
                    self._take()
                self._take("kw", "in")
                values = self._list()
                if len(values) == 1:
                    # `x IN (v)` and `x = v` are the same condition, and
                    # MariaDB re-prints the former as the latter.
                    left = ("cmp", "<>" if negated else "=", left, values[0])
                else:
                    left = ("not_in" if negated else "in", left, values)
            else:
                return left

    def _list(self):
        self._take("op", "(")
        items = [self._operand()]
        while self._at("op", ","):
            self._take()
            items.append(self._operand())
        self._take("op", ")")
        return tuple(items)

    def _operand(self):
        kind, value = self._peek()
        if kind in ("str", "num"):
            self._take()
            return (kind, value)
        if (kind, value) == ("kw", "null"):
            self._take()
            return ("null",)
        if kind == "name":
            self._take()
            if self._at("op", "("):
                return ("call", value, self._list())
            return ("name", value)
        if (kind, value) == ("op", "("):
            self._take()
            inner = self._or()
            self._take("op", ")")
            return inner
        raise _CheckSyntaxError(f"unexpected {value!r}")


def normalize_check(sqltext, dialect_name: str = ""):
    """A CHECK expression as a comparable syntax tree, or None when it
    cannot be parsed (which every caller treats as a mismatch).

    The same parser is used for every dialect. SQLite hands back the text
    as written; MySQL/MariaDB re-print it (lowercase keywords, backticks,
    redundant parentheses, a charset introducer before literals) - all of
    that is absorbed by the tokenizer and by parsing, NOT by discarding
    parentheses: logical grouping survives, so `A OR (B AND C)` never
    equals `(A OR B) AND C`."""
    if sqltext is None:
        return None
    try:
        return _CheckParser(_tokenize_check(sqltext)).parse()
    except (_CheckSyntaxError, RecursionError):
        return None


def _ondelete_matches(live, expected: str, dialect_name: str) -> bool:
    live_rule = (live or "").upper() or None
    if live_rule == expected.upper():
        return True
    # InnoDB treats NO ACTION and RESTRICT identically and SHOW CREATE TABLE
    # may omit either one, so "nothing reported" is RESTRICT there - CASCADE
    # and SET NULL are always reported explicitly and never accepted here.
    return _is_mysql(dialect_name) and expected.upper() == "RESTRICT" and live_rule in (None, "NO ACTION")


# ------------------------------------------------------------------ inspect
def _expected_uniques(table) -> list[tuple[str, tuple]]:
    """(name, ordered columns) of every uniqueness the model declares,
    whether written as a UniqueConstraint or as a unique Index."""
    out = []
    for constraint in table.constraints:
        if constraint.__class__.__name__ == "UniqueConstraint":
            out.append((constraint.name, tuple(c.name for c in constraint.columns)))
    for index in table.indexes:
        if index.unique:
            out.append((index.name, tuple(c.name for c in index.columns)))
    return out


def _expected_checks(table) -> dict[str, str]:
    return {
        c.name: str(c.sqltext) for c in table.constraints
        if c.__class__.__name__ == "CheckConstraint" and c.name
    }


def _inspect_columns(inspector, table, dialect, problems: list[str]) -> None:
    name = table.name
    try:
        live_columns = {c["name"]: c for c in inspector.get_columns(name)}
        pk_columns = list(inspector.get_pk_constraint(name).get("constrained_columns") or [])
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{name}: could not read columns/primary key: {exc}")
        return

    expected_names = [c.name for c in table.columns]
    for extra in sorted(set(live_columns) - set(expected_names)):
        problems.append(f"{name}.{extra}: unexpected column")

    expected_pk = [c.name for c in table.primary_key.columns]
    if sorted(pk_columns) != sorted(expected_pk):
        problems.append(f"{name}: primary key is {sorted(pk_columns)}, expected {sorted(expected_pk)}")

    for column in table.columns:
        live = live_columns.get(column.name)
        if live is None:
            problems.append(f"{name}.{column.name}: column is missing")
            continue

        expected_type = normalize_type(column.type.dialect_impl(dialect), dialect.name)
        live_type = normalize_type(live["type"], dialect.name)
        if live_type != expected_type:
            problems.append(f"{name}.{column.name}: type is {live_type}, expected {expected_type}")

        # A primary-key column is NOT NULL by definition, but SQLite reports
        # nullable=True for it unless the DDL also said NOT NULL - so
        # nullability is only compared for the other columns.
        if not column.primary_key and bool(live.get("nullable", True)) != bool(column.nullable):
            problems.append(
                f"{name}.{column.name}: nullable is {live.get('nullable')!r}, expected {column.nullable!r}"
            )

        expected_default = normalize_default(column.server_default)
        live_default = normalize_default(live.get("default"))
        if live_default != expected_default:
            problems.append(
                f"{name}.{column.name}: server default is {live_default!r}, expected {expected_default!r}"
            )

        # Auto-increment: only MySQL/MariaDB report it reliably. On SQLite a
        # sole INTEGER primary key IS the rowid, so the type comparison above
        # already covers the surrogate ids there.
        if _is_mysql(dialect.name) and column.primary_key:
            expected_auto = column.autoincrement is True
            live_auto = live.get("autoincrement") is True
            if live_auto != expected_auto:
                problems.append(
                    f"{name}.{column.name}: autoincrement is {live_auto!r}, expected {expected_auto!r}"
                )


def _inspect_uniques_and_indexes(inspector, table, problems: list[str]) -> None:
    name = table.name
    try:
        live_constraints = inspector.get_unique_constraints(name)
        live_indexes = inspector.get_indexes(name)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{name}: could not read unique constraints/indexes: {exc}")
        return

    # Uniqueness surfaces as a unique constraint (SQLite) or as a unique
    # index (MySQL/MariaDB reports the same one both ways) - either is real
    # enforcement, so both are collected and compared as ORDERED column
    # tuples. The primary key is reported by neither call, so it never
    # counts as an "extra" unique.
    live_unique: dict[tuple, str] = {}
    for constraint in live_constraints:
        live_unique.setdefault(tuple(constraint.get("column_names") or []), constraint.get("name") or "(unnamed)")
    for index in live_indexes:
        if index.get("unique"):
            live_unique.setdefault(tuple(index.get("column_names") or []), index.get("name") or "(unnamed)")

    # EXACT set. A missing unique lets duplicates in; an extra one is just
    # as wrong the other way - the schema would report ready and then
    # reject rows the durable writers are entitled to store.
    expected = _expected_uniques(table)
    expected_columns = {columns for _, columns in expected}
    for unique_name, columns in expected:
        if columns not in live_unique:
            problems.append(f"{name}: unique {unique_name} on {list(columns)} is missing")
    for columns in sorted(set(live_unique) - expected_columns):
        problems.append(f"{name}: unexpected unique {live_unique[columns]} on {list(columns)}")

    # Plain indexes: only the required ones are checked. An extra
    # non-unique index cannot change what a writer is allowed to store.
    live_index_columns = {tuple(i.get("column_names") or []) for i in live_indexes}
    for index in table.indexes:
        if index.unique:
            continue
        expected_index = tuple(c.name for c in index.columns)
        if expected_index not in live_index_columns:
            problems.append(f"{name}: index {index.name} on {list(expected_index)} is missing")


def _inspect_foreign_keys(inspector, table, dialect, problems: list[str]) -> None:
    name = table.name
    try:
        live_fks = inspector.get_foreign_keys(name)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{name}: could not read foreign keys: {exc}")
        return
    live_by_shape = {
        (tuple(fk.get("constrained_columns") or []), fk.get("referred_table"),
         tuple(fk.get("referred_columns") or [])): fk
        for fk in live_fks
    }
    expected_shapes = set()
    for constraint in table.foreign_key_constraints:
        shape = (
            tuple(c.name for c in constraint.columns),
            constraint.referred_table.name,
            tuple(e.column.name for e in constraint.elements),
        )
        expected_shapes.add(shape)
        live = live_by_shape.get(shape)
        if live is None:
            problems.append(f"{name}: foreign key {list(shape[0])} -> {shape[1]}{list(shape[2])} is missing")
            continue
        expected_rule = constraint.ondelete or "NO ACTION"
        live_rule = (live.get("options") or {}).get("ondelete")
        if not _ondelete_matches(live_rule, expected_rule, dialect.name):
            problems.append(
                f"{name}: foreign key {list(shape[0])} is ON DELETE {live_rule or 'default'}, "
                f"expected {expected_rule}"
            )
    for shape in sorted(set(live_by_shape) - expected_shapes):
        problems.append(f"{name}: unexpected foreign key {list(shape[0])} -> {shape[1]}")


def _inspect_checks(inspector, table, dialect, problems: list[str]) -> None:
    name = table.name
    expected = _expected_checks(table)
    # Read even for a table that declares no CHECK: one that is there
    # anyway is a restriction nobody designed.
    try:
        live_rows = inspector.get_check_constraints(name)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{name}: could not read check constraints: {exc}")
        return

    live: dict[str, str] = {}
    for row in live_rows:
        check_name = row.get("name")
        if not check_name or check_name in live:
            # Unnamed, or the same name twice - neither can be matched to
            # the model, so it is reported rather than guessed at.
            problems.append(f"{name}: unexpected check {check_name or '(unnamed)'}: {row.get('sqltext')!r}")
            continue
        live[check_name] = row.get("sqltext")

    # EXACT set of names...
    for check_name in sorted(set(expected) - set(live)):
        problems.append(f"{name}: check {check_name} is missing")
    for check_name in sorted(set(live) - set(expected)):
        problems.append(f"{name}: unexpected check {check_name}: {live[check_name]!r}")
    # ...and for each expected one, the same parsed expression.
    for check_name in sorted(set(expected) & set(live)):
        expected_tree = normalize_check(expected[check_name], dialect.name)
        live_tree = normalize_check(live[check_name], dialect.name)
        if expected_tree is None or live_tree is None or expected_tree != live_tree:
            problems.append(
                f"{name}: check {check_name} has a different expression: {live[check_name]!r}"
            )


def inspect_schema(engine, tables=None) -> list[str]:
    """Every difference between the given tables' models (default: the
    provisioning tables) and the live database, as human-readable strings. Empty list == the database really
    carries the schema. Anything that cannot be verified is itself a
    problem - "could not check" is never treated as "fine"."""
    dialect = engine.dialect
    if dialect.name not in SUPPORTED_DIALECTS:
        return [f"dialect {dialect.name!r} is not supported by the provisioning schema check"]

    problems: list[str] = []
    inspector = inspect(engine)
    try:
        live_tables = set(inspector.get_table_names())
    except Exception as exc:  # noqa: BLE001
        return [f"could not list tables: {exc}"]

    for table in (mp.PROVISIONING_TABLES if tables is None else tables):
        if table.name not in live_tables:
            problems.append(f"{table.name}: table is missing")
            continue
        _inspect_columns(inspector, table, dialect, problems)
        _inspect_uniques_and_indexes(inspector, table, problems)
        _inspect_foreign_keys(inspector, table, dialect, problems)
        _inspect_checks(inspector, table, dialect, problems)
    return problems


# --------------------------------------------------------------------- seed
def create_tables(engine) -> None:
    """Creates whichever of the ten tables do not exist yet. Existing ones
    are left exactly as they are - inspect_schema() judges them."""
    mp.PROVISIONING_METADATA.create_all(bind=engine, tables=list(mp.PROVISIONING_TABLES))


def seed_fixed_rows(db) -> None:
    """The two tables with a fixed set of rows from day one, added in ONE
    transaction so a failure never leaves half of them behind:

    - provisioning_runtime_state: the singleton, at its inert defaults, with
      a random installation_uuid generated once. An existing row is never
      rewritten - the uuid is the installation's identity.
    - provisioning_type_modes: one 'legacy' row per operation_type.

    Idempotent. If another process seeded the same rows at the same moment,
    this transaction loses on a primary key, is rolled back, and the
    winner's rows are what validate_seed() then looks at."""
    now = dt.datetime.utcnow()
    if db.get(mp.ProvisioningRuntimeState, RUNTIME_STATE_ID) is None:
        db.add(mp.ProvisioningRuntimeState(
            id=RUNTIME_STATE_ID,
            installation_uuid=str(uuid.uuid4()),
            gate_mode_changed_at=now,
            changed_at=now,
        ))
    existing = {row.operation_type for row in db.query(mp.ProvisioningTypeMode.operation_type)}
    for operation_type in mp.OPERATION_TYPES:
        if operation_type not in existing:
            db.add(mp.ProvisioningTypeMode(operation_type=operation_type, mode="legacy", changed_at=now))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


def _is_canonical_uuid(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def validate_seed(db) -> list[str]:
    """The fixed rows must be exactly what L0 writes: one singleton with a
    canonical uuid and inert values, and exactly the eight 'legacy' type
    rows. Anything extra, missing or different is a problem."""
    problems: list[str] = []
    rows = db.query(mp.ProvisioningRuntimeState).all()
    if len(rows) != 1 or rows[0].id != RUNTIME_STATE_ID:
        problems.append(
            f"provisioning_runtime_state: expected exactly the singleton id={RUNTIME_STATE_ID}, "
            f"found ids {sorted(r.id for r in rows)}"
        )
    else:
        row = rows[0]
        if not _is_canonical_uuid(row.installation_uuid):
            problems.append("provisioning_runtime_state: installation_uuid is not a canonical UUID")
        for column, expected in RUNTIME_STATE_L0_VALUES.items():
            if getattr(row, column) != expected:
                problems.append(
                    f"provisioning_runtime_state.{column}: is {getattr(row, column)!r}, expected {expected!r}"
                )
        if row.version is None or row.version < 0:
            problems.append("provisioning_runtime_state.version: is not a non-negative integer")

    modes = {row.operation_type: row for row in db.query(mp.ProvisioningTypeMode)}
    for operation_type in mp.OPERATION_TYPES:
        row = modes.get(operation_type)
        if row is None:
            problems.append(f"provisioning_type_modes: row for {operation_type} is missing")
        elif row.mode != "legacy":
            problems.append(f"provisioning_type_modes.{operation_type}: mode is {row.mode!r}, expected 'legacy'")
    for unknown in sorted(set(modes) - set(mp.OPERATION_TYPES)):
        problems.append(f"provisioning_type_modes: unknown operation_type {unknown!r}")
    return problems


# ---------------------------------------------------------------- bootstrap
def bootstrap(engine, session_factory) -> dict:
    """Create -> inspect -> seed -> validate, recording the outcome for
    is_ready()/assert_ready(). Called once from main.py at startup.

    Never raises. Each stage only runs if the one before it left no
    problem, so nothing is seeded into a schema that failed verification
    and readiness is only ever True after all four stages passed."""
    problems: list[str] = []
    stage = "create"
    try:
        create_tables(engine)
        stage = "inspect"
        problems = inspect_schema(engine)
        if not problems:
            stage = "seed"
            db = session_factory()
            try:
                seed_fixed_rows(db)
                stage = "validate"
                problems = validate_seed(db)
            finally:
                db.close()
    except Exception as exc:  # noqa: BLE001
        # The traceback goes to the log; the stored problem is the exception
        # type and message only (no SQL parameters are involved in any of
        # these stages beyond the seed's own fixed values).
        logger.exception("provisioning schema bootstrap failed during %s", stage)
        problems = problems + [f"bootstrap failed during {stage}: {exc.__class__.__name__}: {exc}"]

    outcome = _record(problems)
    if problems:
        logger.error(
            "provisioning schema is NOT ready - every operation type stays on its legacy path "
            "(%d problem(s)): %s", len(problems), "; ".join(problems[:20]),
        )
    return outcome


def mark_not_ready(reason: str) -> dict:
    """For the caller's own last-resort guard around bootstrap()."""
    return _record([reason])
