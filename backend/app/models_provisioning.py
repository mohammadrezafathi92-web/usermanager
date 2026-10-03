"""Schema for durable provisioning (Lifecycle/P6), rollout batch L0.

Design: docs/lifecycle-provisioning-design-2026-10-01.md (v7.1, frozen),
section 5. This module is SCHEMA ONLY - ten brand-new tables, their
constraints and indexes. Nothing in the application reads or writes these
tables yet except services/provisioning_schema.py, which seeds the two
fixed-row tables and verifies at startup that the database really carries
every table/column/unique/check declared here.

Rules this file follows (section 4 of the design):

- No existing table is touched: no column, index or constraint is added to
  any model in models.py, and nothing here has a ForeignKey to an existing
  table. References to users/admins/nodes/packages/purchases/connections
  are plain integer snapshots on purpose - SQLite in this project does not
  enforce foreign keys (database.py only sets WAL + busy_timeout), so a
  real FK to a deletable table would be a promise only MariaDB keeps.
- Every invariant is a NAMED CheckConstraint/UniqueConstraint. Names are
  what the startup inspector compares against, and auto-migration only
  warns when an index fails to build, so "declared here" is never taken
  as "exists in the database" without that check.
- Integer primary keys that are not surrogate ids (the singleton row, the
  per-node rows) are autoincrement=False: MariaDB rejects a CHECK that
  refers to an AUTO_INCREMENT column, and node_id = 0 is a real row in
  provisioning_gate_stats that AUTO_INCREMENT would silently renumber.
- Enums are VARCHAR + CHECK ... IN (...), JSON is TEXT, money is BIGINT.
"""
import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import mysql

from .database import Base


def _now():
    return dt.datetime.utcnow()


def _pk():
    """Surrogate id: BIGINT AUTO_INCREMENT on MariaDB, INTEGER (rowid alias)
    on SQLite - SQLite only autoincrements a column typed exactly INTEGER."""
    return Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)


def _bigint_fk(target: str):
    return Column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey(target, ondelete="RESTRICT"),
        nullable=False,
    )


def _dt6():
    """DATETIME(6) on MySQL/MariaDB (microseconds - plain DATETIME there
    truncates to whole seconds), ordinary DATETIME on SQLite."""
    return DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql", "mariadb")


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


OPERATION_TYPES = (
    "create_user", "purchase", "add_connection", "renew_user", "renew_purchase",
    "delete_connection", "delete_purchase", "delete_user",
)
APPROVAL_OPERATION_TYPES = ("create_user", "purchase", "renew_user", "renew_purchase")
OPERATION_STATES = (
    "prepared", "provisioning", "remote_complete", "completed",
    "compensating", "compensated", "cleanup_required",
)
ACTOR_KINDS = ("admin", "bot", "system")
STEP_DIRECTIONS = ("create", "remove")
STEP_BACKENDS = (
    "mikrotik_wg", "radius_ppp", "xray_ssh", "threexui", "softether",
    "marzban", "hiddify", "marzneshin", "sui",
)
CONTRACT_BACKENDS = tuple(b for b in STEP_BACKENDS if b != "radius_ppp")
STEP_STATES = (
    "staged", "remote_calling", "remote_created", "active",
    "compensating", "removed", "cleanup_required",
)
REMOTE_OUTCOMES = ("verified_absent", "delete_idempotently_absent", "unverified", "abandoned")
PAYER_KINDS = ("customer_wallet", "reseller_credit")
RESERVATION_GENERATIONS = ("legacy_balance", "wallet_lot", "admin_balance")
RESERVATION_STATES = ("reserved", "captured", "released")
TYPE_MODES = ("legacy", "durable")
CLAIM_KINDS = ("user", "telegram")
LOCK_BACKENDS = ("none", "flock", "flock+get_lock")
GATE_MODES = ("off", "shadow", "enforced")
OWNER_STATES = ("none", "active", "draining", "released")
RECONCILIATION_RESULTS = ("in_sync", "repaired", "router_wider", "conflict", "unreachable")
CONTRACT_STATES = ("unverified", "ready", "invalidated")
INVALIDATION_REASONS = (
    "adapter_version_changed", "config_changed", "server_changed", "backend_changed",
    "probe_mismatch", "manual",
)
OWNERSHIP_EVENT_TYPES = (
    "verify", "claim", "drain_start", "drain_cancel", "release", "shutdown_release",
    "reclaim", "forced_takeover", "gate_mode_change",
    "installation_rotate", "installation_fork_reset",
)
OWNERSHIP_EVENTS_REQUIRING_REASON = (
    "forced_takeover", "reclaim", "installation_rotate", "installation_fork_reset",
)


class ProvisioningOperation(Base):
    """One row per business operation (design 5.1). business_key, never the
    actor, is what makes a retried request land on the same row."""

    __tablename__ = "provisioning_operations"
    __table_args__ = (
        UniqueConstraint("operation_type", "business_key", name="uq_provop_type_business_key"),
        # NULL-exempt on purpose: only a non-terminal create_user holds a name.
        UniqueConstraint("username_claim", name="uq_provop_username_claim"),
        # NULL-exempt on purpose: operations without an approval have both NULL.
        UniqueConstraint("approval_uuid", "approval_execution_version", name="uq_provop_approval_version"),
        Index("ix_provop_state_retry", "state", "next_retry_at"),
        CheckConstraint(_in("operation_type", OPERATION_TYPES), name="ck_provop_operation_type"),
        CheckConstraint(_in("actor_kind", ACTOR_KINDS), name="ck_provop_actor_kind"),
        CheckConstraint(_in("state", OPERATION_STATES), name="ck_provop_state"),
        CheckConstraint(
            "(approval_uuid IS NULL) = (approval_execution_version IS NULL)",
            name="ck_provop_approval_version_pair",
        ),
        CheckConstraint(
            "(approval_uuid IS NULL) = (approval_key_bound IS NULL)",
            name="ck_provop_approval_bound_pair",
        ),
        CheckConstraint(
            "(approval_key_bound IS NOT NULL AND approval_key_bound = 1) = (execution_key_instance_uuid IS NOT NULL)",
            name="ck_provop_key_instance",
        ),
        CheckConstraint(
            f"approval_uuid IS NULL OR {_in('operation_type', APPROVAL_OPERATION_TYPES)}",
            name="ck_provop_approval_types",
        ),
        CheckConstraint(
            "optional_effects IS NULL OR (approval_uuid IS NOT NULL AND state = 'completed')",
            name="ck_provop_optional_effects",
        ),
        CheckConstraint(
            "(state IN ('completed', 'compensated')) = (completed_at IS NOT NULL)",
            name="ck_provop_completed_at",
        ),
        CheckConstraint(
            "state NOT IN ('completed', 'compensated') OR username_claim IS NULL",
            name="ck_provop_terminal_no_claim",
        ),
    )

    id = _pk()
    operation_type = Column(String(24), nullable=False)
    business_key = Column(String(160), nullable=False)
    request_hash = Column(CHAR(64), nullable=False)
    tenant_scope_key = Column(String(64), nullable=False)
    actor_kind = Column(String(8), nullable=False)
    actor_id = Column(Integer, nullable=True)
    actor_ip = Column(String(64), nullable=True)
    approval_uuid = Column(CHAR(36), nullable=True)
    approval_execution_version = Column(Integer, nullable=True)
    approval_key_bound = Column(Boolean, nullable=True)
    execution_key_instance_uuid = Column(CHAR(36), nullable=True)
    optional_effects = Column(Text, nullable=True)
    intent = Column(Text, nullable=False)
    target_user_id = Column(Integer, nullable=True)
    username_claim = Column(String(64), nullable=True)
    state = Column(String(20), nullable=False)
    forward_deadline = Column(_dt6(), nullable=False)
    result_user_id = Column(Integer, nullable=True)
    result_purchase_id = Column(Integer, nullable=True)
    sale_ledger_entry_id = Column(Integer, nullable=True)
    error_code = Column(String(48), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    next_retry_at = Column(_dt6(), nullable=True)
    wallet_epoch_at_start = Column(BigInteger, nullable=False)
    created_at = Column(_dt6(), nullable=False, default=_now)
    completed_at = Column(_dt6(), nullable=True)
    version = Column(Integer, nullable=False, default=0, server_default=text("0"))


class ProvisioningStep(Base):
    """One row per connection slot of an operation (design 5.2): the remote
    identity, the staged credential until the step is terminal, and the
    step's own state."""

    __tablename__ = "provisioning_steps"
    __table_args__ = (
        UniqueConstraint("operation_id", "slot_key", name="uq_provstep_operation_slot"),
        Index("ix_provstep_node_backend_state", "node_id", "backend", "state"),
        Index("ix_provstep_state_retry", "state", "next_retry_at"),
        CheckConstraint(_in("direction", STEP_DIRECTIONS), name="ck_provstep_direction"),
        CheckConstraint(_in("backend", STEP_BACKENDS), name="ck_provstep_backend"),
        CheckConstraint(_in("state", STEP_STATES), name="ck_provstep_state"),
        CheckConstraint(
            f"remote_outcome IS NULL OR {_in('remote_outcome', REMOTE_OUTCOMES)}",
            name="ck_provstep_remote_outcome",
        ),
        CheckConstraint(
            "state NOT IN ('active', 'removed') OR "
            "(staged_wg_private_key IS NULL AND staged_password IS NULL AND staged_xr_uuid IS NULL)",
            name="ck_provstep_terminal_no_secret",
        ),
        CheckConstraint(
            "state NOT IN ('compensating', 'cleanup_required') OR "
            "(staged_wg_private_key IS NULL AND staged_password IS NULL)",
            name="ck_provstep_compensating_no_secret",
        ),
        CheckConstraint(
            "remote_outcome IS NULL OR state IN ('removed', 'cleanup_required')",
            name="ck_provstep_outcome_states",
        ),
        CheckConstraint(
            "state <> 'removed' OR remote_attempted = 0 OR "
            "(remote_outcome IS NOT NULL AND "
            "remote_outcome IN ('verified_absent', 'delete_idempotently_absent', 'abandoned'))",
            name="ck_provstep_removed_needs_outcome",
        ),
        CheckConstraint(
            "state <> 'removed' OR remote_outcome IS NULL OR remote_outcome <> 'unverified'",
            name="ck_provstep_removed_not_unverified",
        ),
        CheckConstraint(
            "state <> 'cleanup_required' OR remote_outcome IS NULL OR remote_outcome = 'unverified'",
            name="ck_provstep_cleanup_outcome",
        ),
        CheckConstraint(
            "state IN ('staged', 'removed', 'active') OR remote_attempted = 1 OR direction = 'remove'",
            name="ck_provstep_attempted_states",
        ),
        CheckConstraint(
            "backend <> 'radius_ppp' OR remote_attempted = 0",
            name="ck_provstep_ppp_no_remote",
        ),
        CheckConstraint(
            "(remote_outcome IS NOT NULL AND remote_outcome = 'abandoned') = (forced_by_admin_id IS NOT NULL)",
            name="ck_provstep_abandoned_forced",
        ),
        CheckConstraint(
            "(forced_by_admin_id IS NULL) = (force_reason IS NULL)",
            name="ck_provstep_force_reason",
        ),
        CheckConstraint(
            "(forced_by_admin_id IS NULL) = (forced_at IS NULL)",
            name="ck_provstep_forced_at",
        ),
        CheckConstraint(
            "state <> 'active' OR connection_id IS NOT NULL",
            name="ck_provstep_active_connection",
        ),
    )

    id = _pk()
    operation_id = _bigint_fk("provisioning_operations.id")
    slot_key = Column(String(64), nullable=False)
    direction = Column(String(8), nullable=False)
    step_order = Column(Integer, nullable=False)
    backend = Column(String(16), nullable=False)
    node_id = Column(Integer, nullable=False)
    protocol = Column(String(16), nullable=False)
    flow = Column(String(64), nullable=False, default="", server_default="")
    # Remote identity (not secret) - fixed in T1, never changed afterwards.
    wg_interface = Column(String(64), nullable=True)
    wg_peer_name = Column(String(128), nullable=True)
    wg_public_key = Column(String(128), nullable=True)
    wg_client_address = Column(String(64), nullable=True)
    wg_gateway_with_prefix = Column(String(64), nullable=True)
    wg_subnet_expanded = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    xr_inbound_tag = Column(String(128), nullable=True)
    xr_panel_inbound_id = Column(Integer, nullable=True)
    xr_email = Column(String(255), nullable=True)
    account_username = Column(String(128), nullable=True)
    speed_limit_mbps = Column(Integer, nullable=True)
    max_concurrent_sessions = Column(Integer, nullable=True)
    # Staged credentials - same trust level as Connection's own columns,
    # NULLed by the transition rules of design 6.3 (enforced by the CHECKs).
    staged_wg_private_key = Column(String(128), nullable=True)
    staged_password = Column(String(128), nullable=True)
    staged_xr_uuid = Column(String(64), nullable=True)
    state = Column(String(20), nullable=False)
    remote_attempted = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    remote_outcome = Column(String(28), nullable=True)
    connection_id = Column(Integer, nullable=True)
    forced_by_admin_id = Column(Integer, nullable=True)
    force_reason = Column(String(500), nullable=True)
    forced_at = Column(_dt6(), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    next_retry_at = Column(_dt6(), nullable=True)
    error_code = Column(String(48), nullable=True)
    error_sanitized = Column(String(500), nullable=True)
    created_at = Column(_dt6(), nullable=False, default=_now)
    updated_at = Column(_dt6(), nullable=False, default=_now)
    version = Column(Integer, nullable=False, default=0, server_default=text("0"))


class PaymentReservation(Base):
    """Structured money reservation of an operation, per payer (design 5.3)."""

    __tablename__ = "payment_reservations"
    __table_args__ = (
        UniqueConstraint("operation_id", "payer_kind", name="uq_payres_operation_payer"),
        UniqueConstraint("hold_ref", name="uq_payres_hold_ref"),
        Index("ix_payres_payer_state", "payer_kind", "state"),
        Index("ix_payres_user_state", "user_id", "state"),
        Index("ix_payres_admin_state", "admin_id", "state"),
        CheckConstraint(_in("payer_kind", PAYER_KINDS), name="ck_payres_payer_kind"),
        CheckConstraint(_in("generation", RESERVATION_GENERATIONS), name="ck_payres_generation"),
        CheckConstraint(_in("state", RESERVATION_STATES), name="ck_payres_state"),
        CheckConstraint("amount > 0", name="ck_payres_amount_positive"),
        CheckConstraint(
            "(payer_kind = 'customer_wallet' AND user_id IS NOT NULL AND admin_id IS NULL "
            "AND generation IN ('legacy_balance', 'wallet_lot')) "
            "OR (payer_kind = 'reseller_credit' AND admin_id IS NOT NULL AND user_id IS NULL "
            "AND generation = 'admin_balance')",
            name="ck_payres_payer_shape",
        ),
        CheckConstraint(
            "(state = 'reserved' AND captured_at IS NULL AND released_at IS NULL "
            "AND capture_ledger_entry_id IS NULL) "
            "OR (state = 'captured' AND captured_at IS NOT NULL AND released_at IS NULL "
            "AND capture_ledger_entry_id IS NOT NULL) "
            "OR (state = 'released' AND released_at IS NOT NULL AND captured_at IS NULL "
            "AND capture_ledger_entry_id IS NULL)",
            name="ck_payres_state_shape",
        ),
    )

    id = _pk()
    operation_id = _bigint_fk("provisioning_operations.id")
    payer_kind = Column(String(20), nullable=False)
    generation = Column(String(16), nullable=False)
    user_id = Column(Integer, nullable=True)
    admin_id = Column(Integer, nullable=True)
    amount = Column(BigInteger, nullable=False)
    hold_ref = Column(CHAR(36), nullable=False)
    state = Column(String(10), nullable=False)
    capture_ledger_entry_id = Column(Integer, nullable=True)
    reserved_at = Column(_dt6(), nullable=False, default=_now)
    captured_at = Column(_dt6(), nullable=True)
    released_at = Column(_dt6(), nullable=True)
    version = Column(Integer, nullable=False, default=0, server_default=text("0"))


class ProvisioningTypeMode(Base):
    """legacy/durable switch, one row per operation_type (design 5.4)."""

    __tablename__ = "provisioning_type_modes"
    __table_args__ = (
        CheckConstraint(_in("mode", TYPE_MODES), name="ck_provmode_mode"),
    )

    operation_type = Column(String(24), primary_key=True)
    mode = Column(String(8), nullable=False, default="legacy", server_default="legacy")
    changed_at = Column(_dt6(), nullable=False, default=_now)
    version = Column(Integer, nullable=False, default=0, server_default=text("0"))


class OneTimePackageClaim(Base):
    """Claim on a one_time_per_user package (design 5.5). release_seq gives
    "only one ACTIVE claim" without leaning on NULL: 0 = active, and a
    released row takes its own id."""

    __tablename__ = "one_time_package_claims"
    __table_args__ = (
        UniqueConstraint("package_id", "claim_key", "release_seq", name="uq_otclaim_package_key_seq"),
        CheckConstraint(_in("claim_kind", CLAIM_KINDS), name="ck_otclaim_kind"),
        CheckConstraint("(release_seq = 0) = (released_at IS NULL)", name="ck_otclaim_release_pair"),
        CheckConstraint(
            "(claim_kind = 'user' AND claim_user_id IS NOT NULL AND claim_tenant_scope_key IS NULL "
            "AND claim_telegram_id IS NULL) "
            "OR (claim_kind = 'telegram' AND claim_user_id IS NULL AND claim_tenant_scope_key IS NOT NULL "
            "AND claim_telegram_id IS NOT NULL)",
            name="ck_otclaim_kind_shape",
        ),
        CheckConstraint("LENGTH(claim_key) = 64", name="ck_otclaim_key_length"),
    )

    id = _pk()
    package_id = Column(Integer, nullable=False)
    claim_key = Column(CHAR(64), nullable=False)
    claim_kind = Column(String(8), nullable=False)
    claim_user_id = Column(Integer, nullable=True)
    claim_tenant_scope_key = Column(String(64), nullable=True)
    claim_telegram_id = Column(BigInteger, nullable=True)
    release_seq = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    operation_id = _bigint_fk("provisioning_operations.id")
    purchase_id = Column(Integer, nullable=True)
    released_at = Column(_dt6(), nullable=True)
    created_at = Column(_dt6(), nullable=False, default=_now)


class ProvisioningRuntimeState(Base):
    """Singleton (id = 1): installation identity, host ownership, lock
    backend and gate mode (design 5.8). L0 only creates the row at its
    inert defaults; nothing reads it yet."""

    __tablename__ = "provisioning_runtime_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_provrt_singleton"),
        CheckConstraint(_in("lock_backend", LOCK_BACKENDS), name="ck_provrt_lock_backend"),
        CheckConstraint(_in("gate_mode", GATE_MODES), name="ck_provrt_gate_mode"),
        CheckConstraint(_in("owner_state", OWNER_STATES), name="ck_provrt_owner_state"),
        CheckConstraint(
            "(owner_state IN ('active', 'draining')) = (owner_host_id IS NOT NULL)",
            name="ck_provrt_owner_host",
        ),
        CheckConstraint(
            "(owner_host_id IS NULL) = (owner_boot_id IS NULL)",
            name="ck_provrt_owner_boot",
        ),
        CheckConstraint(
            "(owner_host_id IS NULL) = (owner_claimed_at IS NULL)",
            name="ck_provrt_owner_claimed",
        ),
        CheckConstraint(
            "(owner_host_id IS NULL) = (owner_heartbeat_at IS NULL)",
            name="ck_provrt_owner_heartbeat",
        ),
        CheckConstraint(
            "(lock_backend = 'none') = (lock_verified_at IS NULL)",
            name="ck_provrt_lock_verified",
        ),
        CheckConstraint(
            "lock_backend <> 'none' OR gate_mode = 'off'",
            name="ck_provrt_gate_needs_lock",
        ),
        CheckConstraint(
            "gate_mode <> 'enforced' OR owner_state IN ('active', 'draining')",
            name="ck_provrt_enforced_needs_owner",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=False)
    installation_uuid = Column(CHAR(36), nullable=False)
    lock_backend = Column(String(24), nullable=False, default="none", server_default="none")
    gate_mode = Column(String(10), nullable=False, default="off", server_default="off")
    gate_mode_epoch = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    gate_mode_changed_at = Column(_dt6(), nullable=False, default=_now)
    owner_state = Column(String(10), nullable=False, default="none", server_default="none")
    owner_host_id = Column(CHAR(64), nullable=True)
    owner_boot_id = Column(CHAR(36), nullable=True)
    ownership_epoch = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    owner_claimed_at = Column(_dt6(), nullable=True)
    owner_heartbeat_at = Column(_dt6(), nullable=True)
    lock_verified_at = Column(_dt6(), nullable=True)
    changed_at = Column(_dt6(), nullable=False, default=_now)
    version = Column(Integer, nullable=False, default=0, server_default=text("0"))


class ProvisioningNodeReconciliation(Base):
    """Last WireGuard interface-address reconciliation result per node
    (design 5.9). One row per node, upserted; no history."""

    __tablename__ = "provisioning_node_reconciliations"
    __table_args__ = (
        CheckConstraint(_in("result", RECONCILIATION_RESULTS), name="ck_provrecon_result"),
    )

    node_id = Column(Integer, primary_key=True, autoincrement=False)
    last_run_at = Column(_dt6(), nullable=False)
    result = Column(String(16), nullable=False)
    db_subnet = Column(String(64), nullable=False)
    router_address = Column(String(64), nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0, server_default=text("0"))
    version = Column(Integer, nullable=False, default=0, server_default=text("0"))


class ProvisioningNodeContract(Base):
    """Durable-readiness of one (node, backend) (design 5.10). An
    'unverified' row carries NO verification data at all - no placeholder
    hash, no fake '{}' - which is exactly what the first CHECK enforces."""

    __tablename__ = "provisioning_node_contracts"
    __table_args__ = (
        # radius_ppp has no remote side, so it never has a contract row.
        CheckConstraint(_in("backend", CONTRACT_BACKENDS), name="ck_provcontract_backend"),
        CheckConstraint(_in("state", CONTRACT_STATES), name="ck_provcontract_state"),
        CheckConstraint(
            f"invalidation_reason IS NULL OR {_in('invalidation_reason', INVALIDATION_REASONS)}",
            name="ck_provcontract_reason",
        ),
        CheckConstraint(
            "state <> 'unverified' OR "
            "(adapter_version IS NULL AND server_fingerprint IS NULL AND config_fingerprint IS NULL "
            "AND contract IS NULL AND verified_by_admin_id IS NULL AND verified_at IS NULL "
            "AND invalidated_at IS NULL AND invalidation_reason IS NULL)",
            name="ck_provcontract_unverified_empty",
        ),
        CheckConstraint(
            "state NOT IN ('ready', 'invalidated') OR "
            "(adapter_version IS NOT NULL AND server_fingerprint IS NOT NULL AND config_fingerprint IS NOT NULL "
            "AND contract IS NOT NULL AND verified_by_admin_id IS NOT NULL AND verified_at IS NOT NULL)",
            name="ck_provcontract_verified_full",
        ),
        CheckConstraint(
            "state <> 'ready' OR (invalidated_at IS NULL AND invalidation_reason IS NULL)",
            name="ck_provcontract_ready_clean",
        ),
        CheckConstraint(
            "state <> 'invalidated' OR (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL)",
            name="ck_provcontract_invalidated_full",
        ),
    )

    node_id = Column(Integer, primary_key=True, autoincrement=False)
    backend = Column(String(16), primary_key=True)
    state = Column(String(12), nullable=False)
    adapter_version = Column(String(40), nullable=True)
    server_fingerprint = Column(CHAR(64), nullable=True)
    config_fingerprint = Column(CHAR(64), nullable=True)
    contract = Column(Text, nullable=True)
    verified_by_admin_id = Column(Integer, nullable=True)
    verified_at = Column(_dt6(), nullable=True)
    invalidated_at = Column(_dt6(), nullable=True)
    invalidation_reason = Column(String(40), nullable=True)
    version = Column(Integer, nullable=False, default=0, server_default=text("0"))


class ProvisioningOwnershipEvent(Base):
    """Permanent audit log of ownership / gate-mode / installation changes
    (design 5.11). Append-only: no code path updates or deletes a row."""

    __tablename__ = "provisioning_ownership_events"
    __table_args__ = (
        Index("ix_provown_created_at", "created_at"),
        CheckConstraint(_in("event_type", OWNERSHIP_EVENT_TYPES), name="ck_provown_event_type"),
        CheckConstraint(
            f"event_type NOT IN ({', '.join(repr(v) for v in OWNERSHIP_EVENTS_REQUIRING_REASON)}) "
            "OR reason IS NOT NULL",
            name="ck_provown_reason_required",
        ),
        CheckConstraint(
            "(event_type = 'gate_mode_change') = (gate_mode_to IS NOT NULL)",
            name="ck_provown_gate_mode_change",
        ),
    )

    id = _pk()
    event_type = Column(String(24), nullable=False)
    from_host_id = Column(CHAR(64), nullable=True)
    to_host_id = Column(CHAR(64), nullable=True)
    ownership_epoch = Column(BigInteger, nullable=False)
    gate_mode_from = Column(String(10), nullable=True)
    gate_mode_to = Column(String(10), nullable=True)
    actor_admin_id = Column(Integer, nullable=False)
    reason = Column(String(500), nullable=True)
    created_at = Column(_dt6(), nullable=False, default=_now)


class ProvisioningGateStat(Base):
    """Best-effort counters per node (design 5.12). node_id = 0 is the
    global row - which is why this key must never be AUTO_INCREMENT."""

    __tablename__ = "provisioning_gate_stats"

    node_id = Column(Integer, primary_key=True, autoincrement=False)
    contention_count = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    writer_not_instrumented_count = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    mode_lock_miss_count = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    timeout_count = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    watchdog_kill_count = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    max_hold_ms = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    last_contention_at = Column(_dt6(), nullable=True)
    last_not_instrumented_at = Column(_dt6(), nullable=True)
    updated_at = Column(_dt6(), nullable=False, default=_now)
    version = Column(Integer, nullable=False, default=0, server_default=text("0"))


PROVISIONING_TABLES = (
    ProvisioningOperation.__table__,
    ProvisioningStep.__table__,
    PaymentReservation.__table__,
    ProvisioningTypeMode.__table__,
    OneTimePackageClaim.__table__,
    ProvisioningRuntimeState.__table__,
    ProvisioningNodeReconciliation.__table__,
    ProvisioningNodeContract.__table__,
    ProvisioningOwnershipEvent.__table__,
    ProvisioningGateStat.__table__,
)
