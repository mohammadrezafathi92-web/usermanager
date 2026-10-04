"""Schema for Receipt Void (docs/receipt-void-design-2026-10-02.md, v16.1
frozen), rollout phase P0: the 36 new tables.

SCHEMA ONLY. Nothing reads or writes these tables yet except
services/receipt_void_schema.py, which creates them, verifies them and
seeds the two singleton rows. No approval is registered, no wallet lot is
created, no behaviour of any existing flow changes.

Like the provisioning tables (models_provisioning.py) these live on their
OWN MetaData, created only inside a never-fatal guard - a DDL problem here
must not stop the panel, the bots or RADIUS from starting. Foreign keys to
the project's existing tables (users, ledger_entries, payment_cards) are
real on MariaDB and reference those tables' Column objects directly.

They are plain Core Table objects, not ORM classes: this phase needs the
DDL and its constraints; the services that will use each table map it when
they are written.

Conventions (design section 4): surrogate ids are BIGINT AUTO_INCREMENT
(INTEGER on SQLite), money is BIGINT toman, timestamps are DATETIME(6),
enums are VARCHAR + CHECK ... IN, JSON is TEXT, every CHECK and UNIQUE is
named, and no UNIQUE relies on NULL except where marked "NULL-exempt".
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
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import mysql

from . import models

RECEIPT_VOID_METADATA = MetaData()
_md = RECEIPT_VOID_METADATA

EFFECT_TYPES = (
    "user_created", "purchase_created", "purchase_renewed", "connection_created", "ledger_sale", "ledger_topup",
    "ledger_credit_spend", "wallet_credit_source_created", "card_payment_recorded", "discount_redeemed",
    "quota_reward_granted", "loyalty_purchase_recorded",
)
APPROVAL_STATES = ("registered", "mutating", "completed", "failed", "cancelled", "voiding", "voided", "cleanup_required")
EVIDENCE_KINDS = ("linked_admin", "owner_seller", "card_approver")
WALLET_OPERATION_TYPES = (
    "void_receipt", "wallet_credit_apply", "wallet_debit", "wallet_refund", "wallet_manual_adjustment",
    "wallet_cache_repair", "wallet_identity_rebind", "approval_recovery", "wallet_hold_resolution",
)
REMOTE_OUTCOMES = ("verified_absent", "delete_idempotently_absent", "unverified", "abandoned")
EFFECT_REVERSAL_STATES = (
    "fully_reversed", "reversed_with_debt", "reversed_with_writeoff", "partially_reversed",
    "unrecoverable", "resource_deleted", "not_applicable",
)
PAYMENT_EVIDENCE_KINDS = ("receipt_approval", "wallet_capture", "admin_credit_spend")


def _now():
    return dt.datetime.utcnow()


def _big():
    return BigInteger().with_variant(Integer, "sqlite")


def _dt6():
    return DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql", "mariadb")


def _pk():
    return Column("id", _big(), primary_key=True, autoincrement=True)


def _fk(name, target, nullable=False, ondelete="RESTRICT", big=True):
    return Column(name, _big() if big else Integer, ForeignKey(target, ondelete=ondelete), nullable=nullable)


def _approval_fk(name="approval_uuid", nullable=False):
    return Column(name, CHAR(36), ForeignKey("receipt_approvals.approval_uuid", ondelete="RESTRICT"), nullable=nullable)


def _created(name="created_at"):
    return Column(name, _dt6(), nullable=False, default=_now)


def _at(name):
    return Column(name, _dt6(), nullable=True)


def _version():
    return Column("version", Integer, nullable=False, default=0, server_default=text("0"))


def _int0(name, big=False):
    return Column(name, BigInteger if big else Integer, nullable=False, default=0, server_default=text("0"))


def _in(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _opt_in(column, values):
    return f"{column} IS NULL OR {_in(column, values)}"


def ck(name, sql):
    return CheckConstraint(sql, name=name)


def uq(name, *columns):
    return UniqueConstraint(*columns, name=name)


# ====================================================================== 5. correlation
receipt_approvals = Table(
    "receipt_approvals", _md,
    _pk(),
    Column("approval_uuid", CHAR(36), nullable=False),
    Column("pending_source_instance_id", String(64), nullable=False),
    Column("pending_local_id", BigInteger, nullable=False),
    _int0("registration_seq"),
    Column("immutable_intent_hash", CHAR(64), nullable=False),
    Column("manifest_hash", CHAR(64), nullable=False),
    Column("supersedes_approval_uuid", CHAR(36), nullable=True),
    Column("cancellation_reason", String(40), nullable=True),
    Column("registered_under_mode", String(10), nullable=False),
    _int0("completeness_generation"),
    Column("registered_by_key_instance_uuid", CHAR(36), nullable=True),
    Column("execution_key_instance_uuid", CHAR(36), nullable=True),
    Column("registered_by_api_key_id", Integer, nullable=True),
    Column("kind", String(8), nullable=False),
    Column("target_shape", String(16), nullable=False),
    Column("approval_mode", String(8), nullable=False),
    _at("auto_granted_at"),
    Column("original_approval_mode", String(8), nullable=False),
    Column("original_approved_by_telegram_id", BigInteger, nullable=True),
    Column("original_approver_evidence_kind", String(20), nullable=True),
    Column("approved_by_admin_id", Integer, nullable=True),
    Column("approved_by_telegram_id", BigInteger, nullable=True),
    Column("approver_evidence_kind", String(20), nullable=True),
    Column("auto_approve_policy_snapshot", Text, nullable=False),
    Column("owner_admin_id_snapshot", Integer, nullable=True),
    Column("tenant_scope_key", String(64), nullable=False),
    Column("telegram_id_snapshot", BigInteger, nullable=True),
    Column("telegram_id_source", String(12), nullable=False),
    Column("target_user_id_snapshot", Integer, nullable=True),
    Column("target_username_snapshot", String(128), nullable=False),
    Column("receipt_file_id_snapshot", String(255), nullable=True),
    Column("amount_snapshot", BigInteger, nullable=False),
    Column("payment_card_id_snapshot", Integer, nullable=True),
    Column("package_id_snapshot", Integer, nullable=True),
    Column("state", String(20), nullable=False),
    _created(), _at("mutating_at"), _at("completed_at"), _at("failed_at"), _at("voided_at"),
    _fk("void_operation_id", "wallet_operations.id", nullable=True),
    _version(),
    uq("uq_rcpt_approval_uuid", "approval_uuid"),
    uq("uq_rcpt_pending_seq", "pending_source_instance_id", "pending_local_id", "registration_seq"),
    Index("ix_rcpt_tenant_state", "tenant_scope_key", "state"),
    Index("ix_rcpt_tenant_telegram_granted", "tenant_scope_key", "telegram_id_snapshot", "auto_granted_at"),
    ck("ck_rcpt_registered_under_mode", _in("registered_under_mode", ("shadow", "required"))),
    ck("ck_rcpt_kind", _in("kind", ("new", "renew", "topup"))),
    ck("ck_rcpt_target_shape", _in("target_shape", ("new_user", "existing_user", "renew_purchase", "renew_user", "topup"))),
    ck("ck_rcpt_approval_mode", _in("approval_mode", ("auto", "manual"))),
    ck("ck_rcpt_original_approval_mode", _in("original_approval_mode", ("auto", "manual"))),
    ck("ck_rcpt_approver_evidence_kind", _opt_in("approver_evidence_kind", EVIDENCE_KINDS)),
    ck("ck_rcpt_telegram_id_source", _in("telegram_id_source", ("user_row", "bot_attested", "none"))),
    ck("ck_rcpt_state", _in("state", APPROVAL_STATES)),
    ck("ck_rcpt_manual_has_approver", "(approval_mode = 'manual') = (approved_by_telegram_id IS NOT NULL)"),
    ck("ck_rcpt_approver_evidence_pair", "(approved_by_telegram_id IS NULL) = (approver_evidence_kind IS NULL)"),
    ck("ck_rcpt_admin_needs_telegram", "approved_by_admin_id IS NULL OR approved_by_telegram_id IS NOT NULL"),
    ck("ck_rcpt_original_manual_has_approver",
       "(original_approval_mode = 'manual') = (original_approved_by_telegram_id IS NOT NULL)"),
    ck("ck_rcpt_original_evidence_pair",
       "(original_approved_by_telegram_id IS NULL) = (original_approver_evidence_kind IS NULL)"),
    ck("ck_rcpt_auto_shape",
       "approval_mode <> 'auto' OR (auto_granted_at IS NOT NULL AND telegram_id_snapshot IS NOT NULL "
       "AND kind IN ('new', 'renew'))"),
    ck("ck_rcpt_telegram_source_none", "(telegram_id_source = 'none') = (telegram_id_snapshot IS NULL)"),
    ck("ck_rcpt_new_user_no_target", "(target_shape = 'new_user') = (target_user_id_snapshot IS NULL)"),
    ck("ck_rcpt_new_user_attested", "(target_shape = 'new_user') = (telegram_id_source = 'bot_attested')"),
    ck("ck_rcpt_seq_supersedes", "(registration_seq = 0) = (supersedes_approval_uuid IS NULL)"),
    ck("ck_rcpt_required_generation", "(registered_under_mode = 'required') = (completeness_generation >= 1)"),
    ck("ck_rcpt_cancelled_reason", "(state = 'cancelled') = (cancellation_reason IS NOT NULL)"),
)

receipt_approval_authority_events = Table(
    "receipt_approval_authority_events", _md,
    _pk(), _approval_fk(),
    Column("reason", String(24), nullable=False),
    Column("from_mode", String(8), nullable=False),
    Column("to_mode", String(8), nullable=False),
    Column("from_key_instance_uuid", CHAR(36), nullable=True),
    Column("to_key_instance_uuid", CHAR(36), nullable=True),
    Column("from_approver_telegram_id", BigInteger, nullable=True),
    Column("to_approver_telegram_id", BigInteger, nullable=True),
    Column("from_approver_evidence_kind", String(20), nullable=True),
    Column("to_approver_evidence_kind", String(20), nullable=True),
    Column("from_state", String(20), nullable=False),
    Column("version_before", Integer, nullable=False),
    Column("version_after", Integer, nullable=False),
    _created(),
    uq("uq_rcptauth_approval_version", "approval_uuid", "version_after"),
    ck("ck_rcptauth_reason", _in("reason", ("retry", "approver_change", "manual_takeover", "revoked_key_takeover"))),
    ck("ck_rcptauth_version_step", "version_after = version_before + 1"),
    ck("ck_rcptauth_modes", "from_mode IN ('auto', 'manual') AND to_mode IN ('auto', 'manual')"),
    ck("ck_rcptauth_from_manual", "(from_mode = 'manual') = (from_approver_telegram_id IS NOT NULL)"),
    ck("ck_rcptauth_to_manual", "(to_mode = 'manual') = (to_approver_telegram_id IS NOT NULL)"),
    ck("ck_rcptauth_from_evidence", "(from_approver_telegram_id IS NULL) = (from_approver_evidence_kind IS NULL)"),
    ck("ck_rcptauth_to_evidence", "(to_approver_telegram_id IS NULL) = (to_approver_evidence_kind IS NULL)"),
    ck("ck_rcptauth_takeover_is_manual",
       "reason NOT IN ('manual_takeover', 'revoked_key_takeover', 'approver_change') OR to_mode = 'manual'"),
)

receipt_approval_effects = Table(
    "receipt_approval_effects", _md,
    _pk(), _approval_fk(),
    Column("effect_type", String(40), nullable=False),
    Column("effect_key", String(160), nullable=False),
    Column("resource_type", String(32), nullable=False),
    Column("resource_id", BigInteger, nullable=True),
    Column("actual_projection", Text, nullable=False),
    Column("resource_snapshot", Text, nullable=False),
    Column("delta_value", BigInteger, nullable=True),
    Column("reversal_state", String(24), nullable=True),
    Column("reversed_value", BigInteger, nullable=True),
    _at("voided_at"),
    _fk("void_operation_id", "wallet_operations.id", nullable=True),
    _created(),
    uq("uq_rcpteff_approval_type_key", "approval_uuid", "effect_type", "effect_key"),
    Index("ix_rcpteff_resource", "resource_type", "resource_id"),
    ck("ck_rcpteff_effect_type", _in("effect_type", EFFECT_TYPES)),
    ck("ck_rcpteff_reversal_state", _opt_in("reversal_state", EFFECT_REVERSAL_STATES)),
    ck("ck_rcpteff_reversal_voided_pair", "(reversal_state IS NULL) = (voided_at IS NULL)"),
    ck("ck_rcpteff_reversal_operation_pair", "(reversal_state IS NULL) = (void_operation_id IS NULL)"),
    ck("ck_rcpteff_value_needs_state", "reversal_state IS NOT NULL OR reversed_value IS NULL"),
    ck("ck_rcpteff_no_value_states",
       "reversal_state NOT IN ('resource_deleted', 'not_applicable') OR reversed_value IS NULL"),
    ck("ck_rcpteff_value_states",
       "reversal_state NOT IN ('fully_reversed', 'reversed_with_debt', 'reversed_with_writeoff', "
       "'partially_reversed', 'unrecoverable') OR reversed_value IS NOT NULL"),
)

receipt_approval_expected_effects = Table(
    "receipt_approval_expected_effects", _md,
    _pk(), _approval_fk(),
    Column("effect_type", String(40), nullable=False),
    Column("effect_key", String(160), nullable=False),
    Column("requirement", String(10), nullable=False),
    Column("expected", Text, nullable=False),
    _created(),
    uq("uq_rcptexp_approval_type_key", "approval_uuid", "effect_type", "effect_key"),
    ck("ck_rcptexp_requirement", _in("requirement", ("required", "optional"))),
)

auto_approval_subjects = Table(
    "auto_approval_subjects", _md,
    Column("tenant_scope_key", String(64), nullable=False),
    Column("telegram_id", BigInteger, nullable=False, autoincrement=False),
    Column("last_granted_at", _dt6(), nullable=False),
    Column("last_approval_uuid", CHAR(36), nullable=False),
    _version(),
    PrimaryKeyConstraint("tenant_scope_key", "telegram_id"),
)

auto_approval_rate_events = Table(
    "auto_approval_rate_events", _md,
    _pk(),
    Column("tenant_scope_key", String(64), nullable=False),
    Column("telegram_id", BigInteger, nullable=False),
    Column("outcome", String(20), nullable=False),
    Column("reason_code", String(32), nullable=True),
    Column("local_decision", String(16), nullable=True),
    Column("approval_uuid", CHAR(36), nullable=True),
    Column("pending_source_instance_id", String(64), nullable=False),
    Column("pending_local_id", BigInteger, nullable=False),
    Column("retry_after_seconds", Integer, nullable=True),
    _created(),
    Index("ix_autorate_subject_created", "tenant_scope_key", "telegram_id", "created_at"),
    ck("ck_autorate_outcome", _in("outcome", ("granted", "rate_limited", "shadow_would_limit", "policy_denied"))),
    ck("ck_autorate_local_decision", _opt_in("local_decision", ("allowed", "denied"))),
)

receipt_approval_runtime_state = Table(
    "receipt_approval_runtime_state", _md,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("registration_mode", String(10), nullable=False, default="off", server_default="off"),
    Column("auto_rate_limit_mode", String(10), nullable=False, default="off", server_default="off"),
    Column("loyalty_timing_mode", String(20), nullable=False, default="legacy_activation",
           server_default="legacy_activation"),
    _int0("completeness_generation"),
    _at("activated_at"),
    Column("key_identity_ready", Boolean, nullable=False, default=False, server_default=text("0")),
    _at("key_identity_checked_at"),
    Column("key_identity_failure", String(40), nullable=True),
    _at("key_identity_failed_at"),
    _created("updated_at"),
    _version(),
    ck("ck_rcptrt_singleton", "id = 1"),
    ck("ck_rcptrt_registration_mode", _in("registration_mode", ("off", "shadow", "required"))),
    ck("ck_rcptrt_auto_rate_limit_mode", _in("auto_rate_limit_mode", ("off", "shadow", "enforced"))),
    ck("ck_rcptrt_loyalty_timing_mode", _in("loyalty_timing_mode", ("legacy_activation", "payment_event"))),
    ck("ck_rcptrt_enforced_needs_required", "auto_rate_limit_mode <> 'enforced' OR registration_mode = 'required'"),
    ck("ck_rcptrt_ready_no_failure", "(key_identity_ready = 1) = (key_identity_failure IS NULL)"),
    ck("ck_rcptrt_required_is_payment_event",
       "(registration_mode = 'required') = (loyalty_timing_mode = 'payment_event')"),
    ck("ck_rcptrt_required_generation", "registration_mode <> 'required' OR completeness_generation >= 1"),
)

receipt_approval_shadow_events = Table(
    "receipt_approval_shadow_events", _md,
    _pk(),
    Column("pending_source_instance_id", String(64), nullable=False),
    Column("pending_local_id", BigInteger, nullable=False),
    Column("stage", String(16), nullable=False),
    Column("error_code", String(48), nullable=False),
    Column("approval_uuid", CHAR(36), nullable=True),
    _created(),
    Index("ix_rcptshadow_created", "created_at"),
    ck("ck_rcptshadow_stage", _in("stage", ("registration", "effect", "finalize"))),
)

# ====================================================================== 7 - 9. operations, locks, runtime
wallet_operations = Table(
    "wallet_operations", _md,
    _pk(),
    Column("operation_type", String(32), nullable=False),
    Column("business_key", String(160), nullable=False),
    Column("request_hash", CHAR(64), nullable=False),
    Column("tenant_scope_key", String(64), nullable=False),
    Column("actor_kind", String(8), nullable=False),
    Column("actor_id", Integer, nullable=True),
    Column("actor_ip", String(64), nullable=True),
    Column("reason", Text, nullable=True),
    Column("state", String(20), nullable=False),
    Column("epoch_at_start", BigInteger, nullable=False),
    Column("result_snapshot", Text, nullable=False),
    _int0("attempts"),
    _at("next_retry_at"), _created(), _at("started_at"), _at("completed_at"),
    _version(),
    uq("uq_walletop_type_business_key", "operation_type", "business_key"),
    Index("ix_walletop_state_retry", "state", "next_retry_at"),
    ck("ck_walletop_operation_type", _in("operation_type", WALLET_OPERATION_TYPES)),
    ck("ck_walletop_actor_kind", _in("actor_kind", ("admin", "bot", "system"))),
    ck("ck_walletop_state", _in("state", ("pending", "running", "completed", "failed", "cleanup_required", "cancelled"))),
)

wallet_operation_steps = Table(
    "wallet_operation_steps", _md,
    _pk(),
    _fk("wallet_operation_id", "wallet_operations.id"),
    Column("step_key", String(160), nullable=False),
    Column("step_order", Integer, nullable=False),
    Column("state", String(12), nullable=False),
    Column("remote_outcome", String(28), nullable=True),
    _int0("attempts"),
    _at("next_retry_at"),
    Column("error_sanitized", Text, nullable=True),
    Column("result_snapshot", Text, nullable=False),
    _created(), _at("completed_at"),
    uq("uq_walletstep_operation_key", "wallet_operation_id", "step_key"),
    Index("ix_walletstep_operation_order", "wallet_operation_id", "step_order"),
    ck("ck_walletstep_state", _in("state", ("pending", "running", "done", "failed", "blocked", "abandoned"))),
    ck("ck_walletstep_remote_outcome", _opt_in("remote_outcome", REMOTE_OUTCOMES)),
    ck("ck_walletstep_state_outcome",
       "(state = 'done' AND (remote_outcome IS NULL OR remote_outcome IN ('verified_absent', 'delete_idempotently_absent'))) "
       # "IS NOT NULL AND" is not redundant: with a NULL outcome the bare
       # comparison is NULL, the whole CHECK is NULL, and SQL lets NULL pass.
       "OR (state = 'blocked' AND remote_outcome IS NOT NULL AND remote_outcome = 'unverified') "
       "OR (state = 'abandoned' AND remote_outcome IS NOT NULL AND remote_outcome = 'abandoned') "
       "OR (state IN ('pending', 'running', 'failed') AND remote_outcome IS NULL)"),
)

resource_locks = Table(
    "resource_locks", _md,
    Column("resource_key", String(96), primary_key=True),
    Column("lease_owner", String(64), nullable=True),
    _int0("fencing_epoch", big=True),
    _at("leased_until"), _at("heartbeat_at"),
    _version(),
)

wallet_runtime_state = Table(
    "wallet_runtime_state", _md,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("phase", String(12), nullable=False, default="normal", server_default="normal"),
    _int0("epoch", big=True),
    Column("external_fencing", String(16), nullable=False, default="none", server_default="none"),
    _created("updated_at"),
    _version(),
    ck("ck_walletrt_singleton", "id = 1"),
    ck("ck_walletrt_phase", _in("phase", ("normal", "fencing", "enforced"))),
    ck("ck_walletrt_external_fencing", _in("external_fencing", ("none", "coordinator"))),
)

# ====================================================================== 10. identity
customer_identities = Table(
    "customer_identities", _md,
    _pk(),
    Column("tenant_scope_key", String(64), nullable=False),
    Column("identity_key", String(96), nullable=False),
    Column("telegram_id", BigInteger, nullable=True),
    Column("owner_admin_id_snapshot", Integer, nullable=True),
    _created(),
    uq("uq_custident_tenant_key", "tenant_scope_key", "identity_key"),
    Index("ix_custident_telegram", "telegram_id"),
)

wallet_accounts = Table(
    "wallet_accounts", _md,
    _pk(),
    Column("lineage_key", CHAR(36), nullable=False),
    _fk("customer_identity_id", "customer_identities.id"),
    Column("user_id", Integer, ForeignKey(models.User.__table__.c.id, ondelete="RESTRICT"), nullable=True),
    Column("user_id_snapshot", Integer, nullable=False),
    Column("username_snapshot", String(128), nullable=False),
    Column("owner_admin_id_snapshot", Integer, nullable=True),
    Column("topup_blocked", Boolean, nullable=False, default=False, server_default=text("0")),
    Column("topup_blocked_reason", String(255), nullable=True),
    _at("tombstoned_at"), _created(), _version(),
    uq("uq_walletacct_lineage_key", "lineage_key"),
    uq("uq_walletacct_user_id", "user_id"),          # NULL-exempt on purpose: only LIVE accounts are unique per user
    Index("ix_walletacct_identity", "customer_identity_id"),
    ck("ck_walletacct_live_or_tombstoned",
       "(user_id IS NOT NULL AND tombstoned_at IS NULL) OR (user_id IS NULL AND tombstoned_at IS NOT NULL)"),
)

wallet_account_identity_rebinds = Table(
    "wallet_account_identity_rebinds", _md,
    _pk(),
    _fk("wallet_account_id", "wallet_accounts.id"),
    _fk("from_identity_id", "customer_identities.id"),
    _fk("to_identity_id", "customer_identities.id"),
    Column("cause", String(20), nullable=False),
    Column("actor_admin_id", Integer, nullable=True),
    _fk("wallet_operation_id", "wallet_operations.id"),
    _created(),
    uq("uq_walletrebind_operation", "wallet_operation_id"),
    Index("ix_walletrebind_account_created", "wallet_account_id", "created_at"),
    ck("ck_walletrebind_cause", _in("cause", ("telegram_link", "telegram_change", "owner_transfer"))),
)

# ====================================================================== 11. money
wallet_credit_sources = Table(
    "wallet_credit_sources", _md,
    _pk(),
    _fk("wallet_account_id", "wallet_accounts.id"),
    Column("origin_kind", String(20), nullable=False),
    Column("source_key", String(160), nullable=False),
    _approval_fk("causation_approval_uuid", nullable=True),
    Column("actor_admin_id", Integer, nullable=True),
    Column("actor_username_snapshot", String(128), nullable=True),
    Column("reason", Text, nullable=True),
    Column("gross_amount", BigInteger, nullable=False),
    Column("unapplied_amount", BigInteger, nullable=False),
    Column("state", String(10), nullable=False),
    _at("voided_at"),
    _fk("void_operation_id", "wallet_operations.id", nullable=True),
    _fk("wallet_operation_id", "wallet_operations.id"),
    _created(), _version(),
    uq("uq_creditsrc_account_origin_key", "wallet_account_id", "origin_kind", "source_key"),
    Index("ix_creditsrc_causation", "causation_approval_uuid"),
    ck("ck_creditsrc_origin_kind", _in("origin_kind", ("receipt_topup", "loyalty_reward", "referral_reward", "manual_adjustment"))),
    ck("ck_creditsrc_state", _in("state", ("applying", "applied", "voiding", "voided"))),
    ck("ck_creditsrc_gross_positive", "gross_amount > 0"),
    ck("ck_creditsrc_unapplied_nonnegative", "unapplied_amount >= 0"),
    ck("ck_creditsrc_topup_has_approval", "origin_kind <> 'receipt_topup' OR causation_approval_uuid IS NOT NULL"),
    ck("ck_creditsrc_manual_has_actor",
       "origin_kind <> 'manual_adjustment' OR (actor_admin_id IS NOT NULL AND reason IS NOT NULL)"),
    ck("ck_creditsrc_applied_fully", "state NOT IN ('applied', 'voided') OR unapplied_amount = 0"),
    ck("ck_creditsrc_void_state_at", "(state IN ('voiding', 'voided')) = (voided_at IS NOT NULL)"),
)

wallet_lots = Table(
    "wallet_lots", _md,
    _pk(),
    _fk("wallet_account_id", "wallet_accounts.id"),
    Column("source_kind", String(24), nullable=False),
    Column("source_key", String(160), nullable=False),
    _fk("source_credit_source_id", "wallet_credit_sources.id", nullable=True),
    Column("original_amount", BigInteger, nullable=False),
    Column("available_amount", BigInteger, nullable=False),
    _int0("consumed_amount", big=True),
    Column("voided_available_amount", BigInteger, nullable=True),
    Column("state", String(10), nullable=False),
    _at("voided_at"),
    _fk("void_operation_id", "wallet_operations.id", nullable=True),
    _created(), _version(),
    uq("uq_walletlot_account_source", "wallet_account_id", "source_kind", "source_key"),
    Index("ix_walletlot_account_state_created", "wallet_account_id", "state", "created_at", "id"),
    Index("ix_walletlot_credit_source", "source_credit_source_id"),
    ck("ck_walletlot_source_kind", _in("source_kind", (
        "receipt_topup", "loyalty_reward", "referral_reward", "manual_adjustment",
        "debt_settlement_remainder", "settlement_reallocation", "legacy_baseline"))),
    ck("ck_walletlot_state", _in("state", ("active", "exhausted", "voided"))),
    ck("ck_walletlot_original_positive", "original_amount > 0"),
    ck("ck_walletlot_available_nonnegative", "available_amount >= 0"),
    ck("ck_walletlot_consumed_nonnegative", "consumed_amount >= 0"),
    ck("ck_walletlot_baseline_no_source", "(source_kind = 'legacy_baseline') = (source_credit_source_id IS NULL)"),
    ck("ck_walletlot_state_shape",
       "(state = 'active' AND voided_at IS NULL AND available_amount > 0 "
       "AND available_amount + consumed_amount = original_amount) "
       "OR (state = 'exhausted' AND voided_at IS NULL AND available_amount = 0 AND consumed_amount = original_amount) "
       "OR (state = 'voided' AND voided_at IS NOT NULL AND available_amount = 0 "
       "AND voided_available_amount IS NOT NULL AND voided_available_amount + consumed_amount = original_amount)"),
)

wallet_debit_events = Table(
    "wallet_debit_events", _md,
    _pk(),
    _fk("wallet_account_id", "wallet_accounts.id"),
    Column("kind", String(20), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("hold_ref", CHAR(36), nullable=False),
    Column("provisioning_ref", String(64), nullable=True),
    Column("ledger_entry_id", Integer, ForeignKey(models.LedgerEntry.__table__.c.id, ondelete="RESTRICT"), nullable=True),
    Column("actor_admin_id", Integer, nullable=True),
    Column("reason", Text, nullable=True),
    Column("state", String(10), nullable=False),
    _created("held_at"), _at("captured_at"), _at("released_at"), _at("reversed_at"),
    _fk("wallet_operation_id", "wallet_operations.id"),
    _created(), _version(),
    uq("uq_walletdebit_hold_ref", "hold_ref"),
    uq("uq_walletdebit_operation", "wallet_operation_id"),
    uq("uq_walletdebit_ledger_entry", "ledger_entry_id"),        # NULL-exempt on purpose
    Index("ix_walletdebit_account_created", "wallet_account_id", "created_at"),
    Index("ix_walletdebit_state_held", "state", "held_at"),
    ck("ck_walletdebit_kind", _in("kind", ("sale", "manual_adjustment"))),
    ck("ck_walletdebit_state", _in("state", ("held", "captured", "released", "reversed"))),
    ck("ck_walletdebit_amount_positive", "amount > 0"),
    ck("ck_walletdebit_kind_shape",
       "(kind = 'sale' AND actor_admin_id IS NULL "
       "AND ((state IN ('held', 'released') AND ledger_entry_id IS NULL) "
       "OR (state IN ('captured', 'reversed') AND ledger_entry_id IS NOT NULL))) "
       "OR (kind = 'manual_adjustment' AND ledger_entry_id IS NULL AND actor_admin_id IS NOT NULL "
       "AND reason IS NOT NULL AND state IN ('captured', 'reversed'))"),
)

wallet_allocations = Table(
    "wallet_allocations", _md,
    _pk(),
    _fk("wallet_debit_event_id", "wallet_debit_events.id"),
    _fk("wallet_lot_id", "wallet_lots.id"),
    Column("allocated_amount", BigInteger, nullable=False),
    _created(),
    uq("uq_walletalloc_debit_lot", "wallet_debit_event_id", "wallet_lot_id"),
    Index("ix_walletalloc_lot", "wallet_lot_id"),
    ck("ck_walletalloc_amount_positive", "allocated_amount > 0"),
)

wallet_allocation_reversals = Table(
    "wallet_allocation_reversals", _md,
    _pk(),
    _fk("wallet_allocation_id", "wallet_allocations.id"),
    Column("reversed_amount", BigInteger, nullable=False),
    Column("lot_was_voided", Boolean, nullable=False),
    _fk("wallet_operation_id", "wallet_operations.id"),
    _created(),
    uq("uq_walletallocrev_allocation", "wallet_allocation_id"),      # an allocation is only ever reversed in full
    ck("ck_walletallocrev_amount_positive", "reversed_amount > 0"),
)

wallet_debts = Table(
    "wallet_debts", _md,
    _pk(),
    _fk("customer_identity_id", "customer_identities.id"),
    _fk("wallet_account_id", "wallet_accounts.id"),
    _fk("source_wallet_lot_id", "wallet_lots.id"),
    _fk("source_void_operation_id", "wallet_operations.id"),
    Column("amount", BigInteger, nullable=False),
    _int0("adjusted_amount", big=True),
    _int0("settled_amount", big=True),
    Column("state", String(20), nullable=False),
    _created(), _version(),
    uq("uq_walletdebt_operation_lot", "source_void_operation_id", "source_wallet_lot_id"),
    Index("ix_walletdebt_identity_state_created", "customer_identity_id", "state", "created_at", "id"),
    ck("ck_walletdebt_state", _in("state", ("open", "partially_settled", "settled"))),
    ck("ck_walletdebt_amount_positive", "amount > 0"),
    ck("ck_walletdebt_adjusted_range", "adjusted_amount >= 0 AND adjusted_amount <= amount"),
    ck("ck_walletdebt_settled_range", "settled_amount >= 0 AND settled_amount <= amount - adjusted_amount"),
    ck("ck_walletdebt_state_shape",
       "(state = 'settled' AND settled_amount = amount - adjusted_amount) "
       "OR (state = 'open' AND settled_amount = 0 AND amount - adjusted_amount > 0) "
       "OR (state = 'partially_settled' AND settled_amount > 0 AND settled_amount < amount - adjusted_amount)"),
)

wallet_credit_applications = Table(
    "wallet_credit_applications", _md,
    _pk(),
    _fk("wallet_credit_source_id", "wallet_credit_sources.id"),
    Column("effect_key", String(160), nullable=False),
    Column("application_kind", String(16), nullable=False),
    Column("amount", BigInteger, nullable=False),
    _int0("reversed_amount", big=True),
    _fk("wallet_lot_id", "wallet_lots.id", nullable=True),
    _fk("wallet_debt_id", "wallet_debts.id", nullable=True),
    _fk("wallet_operation_id", "wallet_operations.id"),
    _created(), _version(),
    uq("uq_creditapp_source_key", "wallet_credit_source_id", "effect_key"),
    uq("uq_creditapp_lot", "wallet_lot_id"),                         # NULL-exempt on purpose: one application per lot
    Index("ix_creditapp_debt_created", "wallet_debt_id", "created_at", "id"),
    ck("ck_creditapp_kind", _in("application_kind", ("lot", "debt_settlement"))),
    ck("ck_creditapp_amount_positive", "amount > 0"),
    ck("ck_creditapp_reversed_range", "reversed_amount >= 0 AND reversed_amount <= amount"),
    ck("ck_creditapp_kind_shape",
       "(application_kind = 'lot' AND wallet_lot_id IS NOT NULL AND wallet_debt_id IS NULL AND reversed_amount = 0) "
       "OR (application_kind = 'debt_settlement' AND wallet_debt_id IS NOT NULL AND wallet_lot_id IS NULL)"),
)

wallet_credit_application_reversals = Table(
    "wallet_credit_application_reversals", _md,
    _pk(),
    _fk("wallet_credit_application_id", "wallet_credit_applications.id"),
    Column("effect_key", String(160), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("disposition", String(16), nullable=False),
    _fk("wallet_operation_id", "wallet_operations.id"),
    _created(),
    uq("uq_creditapprev_application_key", "wallet_credit_application_id", "effect_key"),
    ck("ck_creditapprev_amount_positive", "amount > 0"),
    ck("ck_creditapprev_disposition", _in("disposition", ("reallocate", "void_writeoff"))),
)

wallet_debt_adjustments = Table(
    "wallet_debt_adjustments", _md,
    _pk(),
    _fk("wallet_debt_id", "wallet_debts.id"),
    _fk("source_allocation_reversal_id", "wallet_allocation_reversals.id"),
    Column("amount", BigInteger, nullable=False),
    _fk("wallet_operation_id", "wallet_operations.id"),
    _created(),
    uq("uq_walletdebtadj_reversal", "source_allocation_reversal_id"),
    Index("ix_walletdebtadj_debt", "wallet_debt_id"),
    ck("ck_walletdebtadj_amount_positive", "amount > 0"),
)

wallet_writeoffs = Table(
    "wallet_writeoffs", _md,
    _pk(),
    _fk("wallet_operation_id", "wallet_operations.id"),
    _fk("customer_identity_id", "customer_identities.id"),
    _fk("wallet_account_id", "wallet_accounts.id"),
    _fk("wallet_lot_id", "wallet_lots.id"),
    Column("amount", BigInteger, nullable=False),
    Column("reason", String(32), nullable=False),
    _created(),
    uq("uq_walletwriteoff_operation_lot", "wallet_operation_id", "wallet_lot_id"),
    Index("ix_walletwriteoff_identity", "customer_identity_id"),
    ck("ck_walletwriteoff_amount_positive", "amount > 0"),
    ck("ck_walletwriteoff_reason", _in("reason", ("third_party_reward_consumed",))),
)

wallet_cache_repair_events = Table(
    "wallet_cache_repair_events", _md,
    _pk(),
    _fk("wallet_account_id", "wallet_accounts.id"),
    Column("old_cached_value", BigInteger, nullable=False),
    Column("new_derived_value", BigInteger, nullable=False),
    _fk("wallet_operation_id", "wallet_operations.id"),
    _created(),
    uq("uq_walletcacherepair_operation", "wallet_operation_id"),
)

# ====================================================================== 14.2 loyalty, payment evidence
loyalty_policy_epochs = Table(
    "loyalty_policy_epochs", _md,
    _pk(),
    Column("threshold", Integer, nullable=False),
    Column("credit_amount", BigInteger, nullable=False),
    Column("quota_bytes", BigInteger, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("policy_version", Integer, nullable=False),
    _created("started_at"), _at("closed_at"),
    Column("actor_admin_id", Integer, nullable=True),
    uq("uq_loyaltyepoch_policy_version", "policy_version"),
    ck("ck_loyaltyepoch_threshold", "threshold >= 0"),
    ck("ck_loyaltyepoch_credit_amount", "credit_amount >= 0"),
    ck("ck_loyaltyepoch_quota_bytes", "quota_bytes >= 0"),
    ck("ck_loyaltyepoch_generation", "generation >= 1"),
)

loyalty_user_baselines = Table(
    "loyalty_user_baselines", _md,
    _pk(),
    Column("user_id", Integer, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("purchase_count_baseline", Integer, nullable=False),
    Column("rewards_given_baseline", Integer, nullable=False),
    _created("captured_at"),
    uq("uq_loyaltybaseline_user_generation", "user_id", "generation"),
    ck("ck_loyaltybaseline_generation", "generation >= 1"),
    ck("ck_loyaltybaseline_purchase_count", "purchase_count_baseline >= 0"),
    ck("ck_loyaltybaseline_rewards_given", "rewards_given_baseline >= 0"),
)

loyalty_user_epoch_anchors = Table(
    "loyalty_user_epoch_anchors", _md,
    _pk(),
    Column("user_id", Integer, nullable=True),
    Column("user_id_snapshot", Integer, nullable=False),
    _fk("epoch_id", "loyalty_policy_epochs.id"),
    Column("starts_after_active_count", Integer, nullable=False),
    _created(),
    uq("uq_loyaltyanchor_user_epoch", "user_id", "epoch_id"),         # NULL-exempt on purpose
    ck("ck_loyaltyanchor_starts_after", "starts_after_active_count >= 0"),
)

_ledger_id = models.LedgerEntry.__table__.c.id

sale_payment_evidence = Table(
    "sale_payment_evidence", _md,
    _pk(),
    Column("sale_ledger_entry_id", Integer, ForeignKey(_ledger_id, ondelete="RESTRICT"), nullable=False),
    Column("tenant_scope_key", String(64), nullable=False),
    Column("evidence_kind", String(20), nullable=False),
    _approval_fk(nullable=True),
    _fk("wallet_debit_event_id", "wallet_debit_events.id", nullable=True),
    Column("credit_spend_ledger_entry_id", Integer, ForeignKey(_ledger_id, ondelete="RESTRICT"), nullable=True),
    Column("wallet_account_id", _big(), nullable=True),
    Column("user_id_snapshot", Integer, nullable=False),
    Column("sale_amount", BigInteger, nullable=False),
    Column("captured_amount", BigInteger, nullable=False),
    _created(),
    uq("uq_payevidence_sale_ledger", "sale_ledger_entry_id"),
    uq("uq_payevidence_approval", "approval_uuid"),                  # NULL-exempt
    uq("uq_payevidence_debit", "wallet_debit_event_id"),             # NULL-exempt
    uq("uq_payevidence_credit_spend", "credit_spend_ledger_entry_id"),   # NULL-exempt
    ck("ck_payevidence_kind", _in("evidence_kind", PAYMENT_EVIDENCE_KINDS)),
    ck("ck_payevidence_sale_amount", "sale_amount > 0"),
    ck("ck_payevidence_captured_amount", "captured_amount > 0"),
    ck("ck_payevidence_amounts_equal", "evidence_kind = 'admin_credit_spend' OR sale_amount = captured_amount"),
    ck("ck_payevidence_kind_shape",
       "(evidence_kind = 'receipt_approval' AND approval_uuid IS NOT NULL AND wallet_debit_event_id IS NULL "
       "AND credit_spend_ledger_entry_id IS NULL) "
       "OR (evidence_kind = 'wallet_capture' AND wallet_debit_event_id IS NOT NULL AND approval_uuid IS NULL "
       "AND credit_spend_ledger_entry_id IS NULL) "
       "OR (evidence_kind = 'admin_credit_spend' AND credit_spend_ledger_entry_id IS NOT NULL "
       "AND approval_uuid IS NULL AND wallet_debit_event_id IS NULL)"),
)

loyalty_purchase_events = Table(
    "loyalty_purchase_events", _md,
    _pk(),
    Column("user_id", Integer, nullable=True),
    Column("user_id_snapshot", Integer, nullable=False),
    _fk("payment_evidence_id", "sale_payment_evidence.id"),
    Column("ledger_entry_id", Integer, ForeignKey(_ledger_id, ondelete="RESTRICT"), nullable=False),
    Column("approval_uuid", CHAR(36), nullable=True),
    Column("eligibility_reason", String(24), nullable=False),
    Column("completeness_generation", Integer, nullable=False),
    Column("state", String(8), nullable=False),
    _at("voided_at"),
    Column("void_operation_id", _big(), nullable=True),
    _created(),
    uq("uq_loyaltypurchase_evidence", "payment_evidence_id"),
    uq("uq_loyaltypurchase_ledger", "ledger_entry_id"),
    Index("ix_loyaltypurchase_user_state", "user_id", "state"),
    Index("ix_loyaltypurchase_approval", "approval_uuid"),
    Index("ix_loyaltypurchase_generation", "completeness_generation"),
    ck("ck_loyaltypurchase_reason", _in("eligibility_reason", ("card_paid", "wallet_captured", "admin_credit_paid"))),
    ck("ck_loyaltypurchase_generation", "completeness_generation >= 1"),
    ck("ck_loyaltypurchase_state", _in("state", ("active", "voided"))),
    ck("ck_loyaltypurchase_voided_at", "(state = 'voided') = (voided_at IS NOT NULL)"),
)

loyalty_reward_events = Table(
    "loyalty_reward_events", _md,
    _pk(),
    Column("user_id", Integer, nullable=True),
    Column("user_id_snapshot", Integer, nullable=False),
    _fk("epoch_id", "loyalty_policy_epochs.id"),
    Column("slot", Integer, nullable=False),
    Column("reearn_generation", Integer, nullable=False),
    Column("required_active_count", Integer, nullable=False),
    _fk("triggering_purchase_event_id", "loyalty_purchase_events.id"),
    Column("causation_type", String(20), nullable=False),
    Column("causation_approval_uuid", CHAR(36), nullable=True),
    Column("causation_wallet_debit_event_id", _big(), nullable=True),
    Column("causation_credit_spend_ledger_entry_id", Integer, nullable=True),
    _int0("credit_amount", big=True),
    _int0("quota_bytes", big=True),
    Column("wallet_credit_source_id", _big(), nullable=True),
    Column("quota_target_type", String(8), nullable=True),
    Column("quota_target_id", Integer, nullable=True),
    Column("quota_before", BigInteger, nullable=True),
    Column("quota_after", BigInteger, nullable=True),
    Column("before_was_unlimited", Boolean, nullable=True),
    Column("credit_reversal_state", String(24), nullable=True),
    Column("credit_reversed_amount", BigInteger, nullable=True),
    Column("quota_reversal_state", String(24), nullable=True),
    Column("quota_reversed_bytes", BigInteger, nullable=True),
    Column("state", String(8), nullable=False),
    Column("void_reason", String(24), nullable=True),
    _at("voided_at"),
    Column("void_operation_id", _big(), nullable=True),
    _created(),
    uq("uq_loyaltyreward_user_epoch_slot", "user_id", "epoch_id", "slot", "reearn_generation"),   # NULL-exempt on purpose
    Index("ix_loyaltyreward_user_state_required", "user_id", "state", "required_active_count"),
    ck("ck_loyaltyreward_slot", "slot >= 1"),
    ck("ck_loyaltyreward_reearn_generation", "reearn_generation >= 1"),
    ck("ck_loyaltyreward_required_active_count", "required_active_count >= 1"),
    ck("ck_loyaltyreward_causation_type", _in("causation_type", PAYMENT_EVIDENCE_KINDS)),
    ck("ck_loyaltyreward_credit_reversal_state", _opt_in("credit_reversal_state", (
        "fully_reversed", "reversed_with_debt", "reversed_with_writeoff", "unrecoverable"))),
    ck("ck_loyaltyreward_quota_reversal_state", _opt_in("quota_reversal_state", (
        "fully_reversed", "partially_reversed", "resource_deleted", "not_applicable"))),
    ck("ck_loyaltyreward_state", _in("state", ("active", "voided"))),
    ck("ck_loyaltyreward_void_reason", _opt_in("void_reason", ("receipt_void", "loyalty_reconciliation", "sale_refund"))),
    ck("ck_loyaltyreward_voided_shape",
       "(state = 'voided') = (voided_at IS NOT NULL AND void_reason IS NOT NULL AND void_operation_id IS NOT NULL)"),
    ck("ck_loyaltyreward_not_empty", "credit_amount > 0 OR quota_bytes > 0"),
    ck("ck_loyaltyreward_credit_reversal_when",
       "(credit_reversal_state IS NOT NULL) = (state = 'voided' AND credit_amount > 0)"),
    ck("ck_loyaltyreward_credit_reversed_pair",
       "(credit_reversed_amount IS NOT NULL) = (credit_reversal_state IS NOT NULL)"),
    ck("ck_loyaltyreward_quota_reversal_when",
       "(quota_reversal_state IS NOT NULL) = (state = 'voided' AND quota_bytes > 0)"),
    ck("ck_loyaltyreward_quota_reversed_pair",
       "(quota_reversed_bytes IS NOT NULL) = "
       "(quota_reversal_state IS NOT NULL AND quota_reversal_state IN ('fully_reversed', 'partially_reversed'))"),
    ck("ck_loyaltyreward_causation_approval",
       "(causation_type = 'receipt_approval') = (causation_approval_uuid IS NOT NULL)"),
    ck("ck_loyaltyreward_causation_debit",
       "(causation_type = 'wallet_capture') = (causation_wallet_debit_event_id IS NOT NULL)"),
    ck("ck_loyaltyreward_causation_spend",
       "(causation_type = 'admin_credit_spend') = (causation_credit_spend_ledger_entry_id IS NOT NULL)"),
)

quota_reversal_shortfalls = Table(
    "quota_reversal_shortfalls", _md,
    _pk(),
    _fk("receipt_approval_effect_id", "receipt_approval_effects.id", nullable=True),
    _fk("loyalty_reward_event_id", "loyalty_reward_events.id", nullable=True),
    _fk("wallet_operation_id", "wallet_operations.id"),
    Column("user_id_snapshot", Integer, nullable=False),
    Column("granted_bytes", BigInteger, nullable=False),
    Column("reversed_bytes", BigInteger, nullable=False),
    Column("shortfall_bytes", BigInteger, nullable=False),
    _created(),
    uq("uq_quotashort_effect", "receipt_approval_effect_id"),        # NULL-exempt on purpose
    uq("uq_quotashort_reward", "loyalty_reward_event_id"),           # NULL-exempt on purpose
    ck("ck_quotashort_granted", "granted_bytes > 0"),
    ck("ck_quotashort_reversed", "reversed_bytes >= 0"),
    ck("ck_quotashort_shortfall", "shortfall_bytes > 0"),
    ck("ck_quotashort_sum", "reversed_bytes + shortfall_bytes = granted_bytes"),
    ck("ck_quotashort_exactly_one_source",
       "(receipt_approval_effect_id IS NULL) <> (loyalty_reward_event_id IS NULL)"),
)

# ====================================================================== 15. payment cards
payment_card_pool_states = Table(
    "payment_card_pool_states", _md,
    Column("pool_key", String(32), primary_key=True),
    Column("logging_phase", String(12), nullable=False),
    _int0("epoch", big=True),
    _created("updated_at"),
    _version(),
    ck("ck_cardpool_logging_phase", _in("logging_phase", ("legacy", "event_logged"))),
)

payment_card_pool_baselines = Table(
    "payment_card_pool_baselines", _md,
    Column("pool_key", String(32), ForeignKey("payment_card_pool_states.pool_key", ondelete="RESTRICT"), primary_key=True),
    Column("accumulated_by_card", Text, nullable=False),
    _created("captured_at"),
)

payment_card_pool_events = Table(
    "payment_card_pool_events", _md,
    _pk(),
    Column("pool_key", String(32), ForeignKey("payment_card_pool_states.pool_key", ondelete="RESTRICT"), nullable=False),
    Column("event_kind", String(28), nullable=False),
    Column("card_id", Integer, ForeignKey(models.PaymentCard.__table__.c.id, ondelete="SET NULL"), nullable=True),
    Column("card_id_snapshot", Integer, nullable=False),
    Column("card_label_snapshot", String(32), nullable=True),
    Column("approval_uuid", CHAR(36), nullable=True),
    Column("amount", BigInteger, nullable=False),
    Column("accumulated_before", BigInteger, nullable=False),
    Column("active_card_id_before", Integer, nullable=True),
    Column("card_order_snapshot", Text, nullable=False),
    Column("reset_after", Boolean, nullable=False, default=False, server_default=text("0")),
    Column("rotated_to_card_id_snapshot", Integer, nullable=True),
    Column("mode_snapshot", String(16), nullable=False),
    Column("threshold_snapshot", BigInteger, nullable=True),
    _at("voided_at"),
    Column("void_operation_id", _big(), nullable=True),
    _created(),
    Index("ix_cardevent_pool_card_id", "pool_key", "card_id_snapshot", "id"),
    uq("uq_cardevent_approval", "approval_uuid"),                    # NULL-exempt on purpose
    ck("ck_cardevent_kind", _in("event_kind", ("payment_recorded", "payment_recorded_uncorrelated"))),
    ck("ck_cardevent_amount_positive", "amount > 0"),
    ck("ck_cardevent_accumulated_nonnegative", "accumulated_before >= 0"),
    ck("ck_cardevent_kind_approval",
       "(event_kind = 'payment_recorded' AND approval_uuid IS NOT NULL) "
       "OR (event_kind = 'payment_recorded_uncorrelated' AND approval_uuid IS NULL)"),
    ck("ck_cardevent_void_only_correlated", "voided_at IS NULL OR event_kind = 'payment_recorded'"),
    ck("ck_cardevent_rotate_needs_reset", "reset_after = 1 OR rotated_to_card_id_snapshot IS NULL"),
    ck("ck_cardevent_reset_shape",
       "reset_after = 0 OR (mode_snapshot = 'threshold' AND threshold_snapshot IS NOT NULL AND threshold_snapshot > 0 "
       "AND accumulated_before + amount >= threshold_snapshot "
       "AND active_card_id_before IS NOT NULL AND active_card_id_before = card_id_snapshot)"),
)

RECEIPT_VOID_TABLES = tuple(RECEIPT_VOID_METADATA.sorted_tables)
