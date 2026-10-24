"""Reviewed compatibility upgrade of node contracts, 42d -> c22 (receipt 1140).

docs/node-contract-compatibility-upgrade.md. c22 changed the global adapter
digest only because adapter_mikrotik_wg.py's cancellation/refund helpers were
split; probe, create, read, identity, matcher and verified-absence code did
not change. Startup therefore invalidated every verified contract with reason
adapter_version_changed. This is NOT a new live verification: it restores
exactly those rows, for exactly that one reviewed digest pair, and only while
the actual current digest is still that target. Every existing guard
(require_ready, p6 readiness, the runner child's ChildGuard) is untouched and
then simply sees a normal current 'ready' row.

Not one of the eight hashed adapter files, and its audit table is on its own
metadata (the ten provisioning tables are a closed list). No node I/O, no DDL
and no commit inside recover_reviewed(): the caller owns the transaction.
"""
import datetime as dt
import json
import re

from sqlalchemy import Column, Integer, LargeBinary, String, Text, UniqueConstraint, cast, insert, select, update
from sqlalchemy.orm import declarative_base

from .. import models, models_provisioning as mp
from . import provisioning_contract_rules as contract_rules
from . import provisioning_contracts as contracts
from . import provisioning_schema
from .adapter_base import InvalidMatcher

SOURCE_VERSION = "742e58935c84e385565c1468b06c6bdd3218e984"   # 42d0ec8, stored in the invalidated rows
TARGET_VERSION = "8342748d18b4c80457d28d1ef038d7389fef3d39"   # c22f1dc, the reviewed current digest
BACKENDS = ("mikrotik_wg", "threexui", "softether")            # the reviewed, unchanged backends only
REASON = "adapter_version_changed"

# Every contract column a recovery observes (and CASes on), in a fixed order.
OBSERVED = ("state", "adapter_version", "server_fingerprint", "config_fingerprint", "contract",
            "verified_by_admin_id", "verified_at", "invalidated_at", "invalidation_reason", "version")
_HEX64 = re.compile(r"[0-9a-f]{64}")

AuditBase = declarative_base()


class UpgradeAudit(AuditBase):
    """One row per recovered contract, written in the same transaction as the
    recovery. Append-only; the unique key makes a replay of the same row
    version impossible."""

    __tablename__ = "provisioning_contract_upgrade_audits"
    __table_args__ = (
        UniqueConstraint("node_id", "backend", "row_version_before", name="uq_provcupgrade_row_version"),
    )

    id = mp._pk()
    node_id = Column(Integer, nullable=False)
    backend = Column(String(16), nullable=False)
    source_version = Column(String(40), nullable=False)
    target_version = Column(String(40), nullable=False)
    row_version_before = Column(Integer, nullable=False)
    row_version_after = Column(Integer, nullable=False)
    before_image = Column(Text, nullable=False)          # the contract row's columns only - no node credential
    recovered_at = Column(mp._dt6(), nullable=False)


def bootstrap(engine) -> list:
    """Create the audit table if missing and inspect it. [] == verified; any
    problem (or error) is reported and means: do not recover."""
    table = UpgradeAudit.__table__
    try:
        AuditBase.metadata.create_all(bind=engine, tables=[table])
        return provisioning_schema.inspect_schema(engine, tables=[table])
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return [f"{table.name}: bootstrap failed ({type(exc).__name__})"]


def _live_backend(node):
    protocol = ("wireguard" if node.type == models.NodeType.mikrotik else
                "softether" if node.type == models.NodeType.softether else "xray")
    try:
        return contracts.backend_for(node, protocol)
    except Exception:  # noqa: BLE001 - an unknown backend is never eligible
        return None


def _eligible(row, node) -> bool:
    if node is None or node.enabled is not True:
        return False
    if (row.state != "invalidated" or row.invalidation_reason != REASON or row.adapter_version != SOURCE_VERSION
            or row.backend not in BACKENDS or _live_backend(node) != row.backend):
        return False
    if not (isinstance(row.server_fingerprint, str) and _HEX64.fullmatch(row.server_fingerprint)):
        return False
    if not (isinstance(row.config_fingerprint, str) and _HEX64.fullmatch(row.config_fingerprint)
            and row.config_fingerprint == contracts.config_fingerprint(node)):
        return False
    if type(row.verified_by_admin_id) is not int or row.verified_by_admin_id <= 0:
        return False
    if not (isinstance(row.verified_at, dt.datetime) and isinstance(row.invalidated_at, dt.datetime)
            and row.verified_at <= row.invalidated_at):
        return False
    if type(row.version) is not int or row.version < 0 or not isinstance(row.contract, str):
        return False
    try:
        contracts.validate_contract(row.contract, row.backend)
    except (ValueError, TypeError, InvalidMatcher):
        return False
    return True


def _image(row) -> str:
    values = {"node_id": row.node_id, "backend": row.backend}
    for name in OBSERVED:
        value = getattr(row, name)
        values[name] = value.isoformat() if isinstance(value, dt.datetime) else value
    return json.dumps(values, sort_keys=True, separators=(",", ":"))


def recover_reviewed(db) -> list:
    """Restore the reviewed-compatible invalidated contracts. Returns
    [(node_id, backend)] in node order. No commit: the caller commits, and a
    caller rollback (or any exception here, e.g. an audit failure) undoes
    every recovery of this call."""
    if contract_rules.adapter_version() != TARGET_VERSION:
        return []          # a future hashed-source change disables this upgrade
    contract = mp.ProvisioningNodeContract
    candidates = db.execute(select(contract.node_id, contract.backend).where(
        contract.state == "invalidated", contract.invalidation_reason == REASON,
        contract.adapter_version == SOURCE_VERSION, contract.backend.in_(BACKENDS))
        .order_by(contract.node_id, contract.backend)).all()
    table = contract.__table__
    recovered = []
    for node_id, backend in candidates:
        # Node lock, then contract lock (fixed order), both fresh.
        node = db.execute(select(models.Node).where(models.Node.id == node_id).with_for_update()
                          .execution_options(populate_existing=True)).scalar_one_or_none()
        row = db.execute(select(contract).where(contract.node_id == node_id, contract.backend == backend)
                         .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if row is None or not _eligible(row, node):
            continue
        observed = {name: getattr(row, name) for name in OBSERVED}
        image = _image(row)
        # A backend's default text collation may ignore case or trailing
        # spaces. Matcher literals do not: compare text as exact UTF-8 bytes.
        def unchanged(column, value):
            if value is None:
                return column.is_(None)
            if isinstance(value, str):
                return cast(column, LargeBinary) == value.encode("utf-8")
            return column == value

        conditions = [table.c.node_id == node_id, unchanged(table.c.backend, backend)]
        conditions += [unchanged(table.c[name], value) for name, value in observed.items()]
        result = db.execute(update(table).where(*conditions).values(
            state="ready", adapter_version=TARGET_VERSION, invalidated_at=None, invalidation_reason=None,
            version=observed["version"] + 1))
        if result.rowcount != 1:
            continue       # lost the CAS to a concurrent writer: nothing written, nothing audited
        db.execute(insert(UpgradeAudit.__table__).values(
            node_id=node_id, backend=backend, source_version=SOURCE_VERSION, target_version=TARGET_VERSION,
            row_version_before=observed["version"], row_version_after=observed["version"] + 1,
            before_image=image, recovered_at=dt.datetime.utcnow()))
        recovered.append((node_id, backend))
    return recovered
