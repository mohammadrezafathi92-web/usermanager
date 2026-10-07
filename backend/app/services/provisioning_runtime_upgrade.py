"""Explicit, narrowly scoped ownership CHECK upgrade. No live/startup caller.

Operator-approved exception to additive-only migrations. A maintenance
controller must authorize the operator, stop new provisioning and retain the
exclusive installation mode lock through this function. Only this singleton
table is changed, only from a verified known v1 schema while inert. No row,
business table, mode, ownership or financial value is changed.
"""
import re

from sqlalchemy import CheckConstraint, MetaData, inspect, select, text
from sqlalchemy.schema import CreateTable

from .. import models, models_provisioning as mp
from . import provisioning_schema as schema, provisioning_runtime_contract as contract
from .provisioning_lock_verification import require_exclusive
from .provisioning_transitions import TERMINAL

TABLE = "provisioning_runtime_state"
TEMP = "provisioning_runtime_state_upgrade_v2"


class UpgradeRefused(RuntimeError):
    pass


def runtime_table(expression, name=TABLE):
    """An exact model clone changing only this named CHECK."""
    table = mp.ProvisioningRuntimeState.__table__.to_metadata(MetaData(), name=name)
    for constraint in list(table.constraints):
        if constraint.name == contract.NAME:
            table.constraints.remove(constraint)
    table.append_constraint(CheckConstraint(expression, name=contract.NAME))
    return table


def _checkpoint(phase):
    """Fault-injection seam; no production callback is accepted."""


def _preflight(connection, installation_uuid, actor_admin_id, expected_version):
    if type(actor_admin_id) is not int or actor_admin_id <= 0 or type(expected_version) is not int:
        raise UpgradeRefused("upgrade_request_invalid")
    if not connection.execute(select(models.AdminUser.is_superadmin).where(
            models.AdminUser.id == actor_admin_id)).scalar_one_or_none():
        raise UpgradeRefused("upgrade_superadmin_required")
    problems = schema.inspect_schema(connection)
    if problems:
        raise UpgradeRefused("upgrade_schema_unverified")
    row = connection.execute(select(mp.ProvisioningRuntimeState.__table__)).mappings().one()
    if row["installation_uuid"] != installation_uuid or row["version"] != expected_version or (
            row["gate_mode"] != "off" or row["owner_state"] not in ("none", "released")):
        raise UpgradeRefused("upgrade_runtime_not_inert")
    if connection.execute(select(mp.ProvisioningTypeMode.operation_type).where(
            mp.ProvisioningTypeMode.mode != "legacy")).first() or connection.execute(select(mp.ProvisioningOperation.id)
            .where(mp.ProvisioningOperation.state.notin_(TERMINAL))).first():
        raise UpgradeRefused("upgrade_operations_not_quiescent")
    inspector = inspect(connection)
    if TEMP in inspector.get_table_names() or inspector.get_indexes(TABLE):
        raise UpgradeRefused("upgrade_unknown_artifacts")
    for name in inspector.get_table_names():
        if any(fk.get("referred_table") == TABLE for fk in inspector.get_foreign_keys(name)):
            raise UpgradeRefused("upgrade_incoming_reference")
    if connection.dialect.name == "sqlite":
        artifacts = connection.execute(text("SELECT sql FROM sqlite_master WHERE type IN ('trigger','view')")).scalars()
        if any(sql and re.search(r"\bprovisioning_runtime_state\b", sql, re.I) for sql in artifacts):
            raise UpgradeRefused("upgrade_unknown_artifacts")
    return dict(row)


def upgrade(engine, installation_uuid, mode_hold, *, actor_admin_id, expected_version, base_dir=None):
    require_exclusive(mode_hold, installation_uuid, base_dir)
    with engine.connect() as connection:
        dialect = connection.dialect
        if dialect.name == "sqlite":
            # Explicit BEGIN makes Python sqlite legacy DDL transactional too.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.begin()
        try:
            before = _preflight(connection, installation_uuid, actor_admin_id, expected_version)
            if contract.supports_release(connection):
                connection.rollback()
                return {"changed": False, "version": 2}
            old = runtime_table(contract.LEGACY)
            if schema.inspect_schema(connection, (old,)):
                raise UpgradeRefused("upgrade_not_known_v1")
            _checkpoint("before_ddl")
            if dialect.name == "sqlite":
                violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                replacement = runtime_table(contract.RELEASED, TEMP)
                connection.execute(CreateTable(replacement))
                columns = ",".join(dialect.identifier_preparer.quote(column.name) for column in old.columns)
                connection.exec_driver_sql(f'INSERT INTO "{TEMP}" ({columns}) SELECT {columns} FROM "{TABLE}"')
                _checkpoint("after_copy")
                copied = connection.execute(select(replacement)).mappings().one()
                if dict(copied) != before:
                    raise UpgradeRefused("upgrade_copy_mismatch")
                connection.exec_driver_sql(f'DROP TABLE "{TABLE}"')
                _checkpoint("after_drop")
                connection.exec_driver_sql(f'ALTER TABLE "{TEMP}" RENAME TO "{TABLE}"')
                if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() != violations:
                    raise UpgradeRefused("upgrade_foreign_keys_changed")
            elif getattr(dialect, "is_mariadb", False) and dialect.server_version_info >= (10, 6):
                # One crash-atomic ALTER, not separate DROP and ADD statements.
                # MariaDB DDL implicitly commits; a fault AFTER this statement
                # cannot be rolled back. Retrying detects the valid v2 schema.
                connection.exec_driver_sql(f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `{contract.NAME}`, "
                    f"ADD CONSTRAINT `{contract.NAME}` CHECK ({contract.RELEASED})")
            else:
                raise UpgradeRefused("upgrade_dialect_unsupported")
            _checkpoint("after_ddl")
            if schema.inspect_schema(connection, (runtime_table(contract.RELEASED),)) or schema.inspect_schema(connection):
                raise UpgradeRefused("upgrade_postcondition_failed")
            after = dict(connection.execute(select(mp.ProvisioningRuntimeState.__table__)).mappings().one())
            if after != before:
                raise UpgradeRefused("upgrade_runtime_changed")
            connection.commit()
            return {"changed": True, "version": 2}
        except BaseException:
            connection.rollback()
            raise
