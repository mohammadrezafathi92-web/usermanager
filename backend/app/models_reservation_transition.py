"""Schema of the P9a reservation transition (docs addendum
receipt-void-addendum-p9a-reservations-2026-10-19.md).

Additive, on its OWN MetaData (like models_receipt_void.py), created and
inspected by services/receipt_void_schema.py. No foreign key on purposes:
a Purchase / User may be deleted normally, and its history must stay.

  loyalty_cutover_purchase_reservations
        immutable snapshot, in the joint activation's transaction, of every
        Purchase reservation that existed when 'payment_event' began. It
        proves what was QUEUED at that instant - not every historical right.
  purchase_reservation_activations
        one row per consumption of a Purchase reservation, written in the
        consuming transaction. Ordered per Purchase by its seq_before (the
        Purchase's reservation_activation_seq the consumer incremented under
        the row lock), never by time: a snapshot taken at seq s is consumed
        by exactly the row (purchase_id, purchase_created_at, seq_before = s).
  product_decision_confirmations
        a superadmin's confirmation of one exact version (content hash) of
        the Product decisions the activation depends on.
  reservation_resolution_decisions / reservation_resolutions
        the owner's explicit per-customer decision about a User reservation,
        and its single execution (idempotency key, audit, same commit).
"""
import datetime as dt

from sqlalchemy import BigInteger, Boolean, CHAR, Column, Integer, MetaData, String, Table, Text

from .models_receipt_void import _big, _dt6, _in, _pk, ck, uq

RESERVATION_TRANSITION_METADATA = MetaData()
_md = RESERVATION_TRANSITION_METADATA

ACTIVATION_TRIGGERS = ("poller", "radius", "other")
RESOLUTION_ACTIONS = ("retire_residual", "transfer_to_purchase")
CAP_POLICIES = ("keep_current_cap", "package_cap")


def _now():
    return dt.datetime.utcnow()


def _created(name="created_at"):
    return Column(name, _dt6(), nullable=False, default=_now)


loyalty_cutover_purchase_reservations = Table(
    "loyalty_cutover_purchase_reservations", _md,
    _pk(),
    Column("generation", Integer, nullable=False),
    Column("purchase_id", Integer, nullable=False),
    # with purchase_id, the purchase's identity: SQLite may give a deleted
    # purchase's id to a new one - history never attaches to the new one.
    Column("purchase_created_at", _dt6(), nullable=False),
    Column("user_id_snapshot", Integer, nullable=False),
    Column("owner_admin_id_snapshot", Integer, nullable=True),
    Column("reserved_quota_bytes", BigInteger, nullable=True),
    Column("reserved_duration_days", Integer, nullable=True),
    Column("reserved_package_id", Integer, nullable=True),
    Column("reserved_max_concurrent_sessions", Integer, nullable=True),
    Column("reserved_replace_package_limits", Boolean, nullable=True),
    Column("reserved_created_at", _dt6(), nullable=True),
    Column("reservation_activation_seq", Integer, nullable=False),
    Column("row_hash", CHAR(64), nullable=False),
    _created(),
    uq("uq_cutoverresv_generation_purchase", "generation", "purchase_id"),
    ck("ck_cutoverresv_generation", "generation >= 1"),
    ck("ck_cutoverresv_seq", "reservation_activation_seq >= 0"),
)

purchase_reservation_activations = Table(
    "purchase_reservation_activations", _md,
    _pk(),
    Column("purchase_id", Integer, nullable=False),
    Column("purchase_created_at", _dt6(), nullable=False),
    Column("user_id_snapshot", Integer, nullable=False),
    Column("seq_before", Integer, nullable=False),
    Column("activated_by", String(16), nullable=False),     # "trigger" is reserved in MySQL
    Column("before_json", Text, nullable=False),
    Column("after_json", Text, nullable=False),
    _created(),
    uq("uq_resvactivation_purchase_seq", "purchase_id", "purchase_created_at", "seq_before"),
    ck("ck_resvactivation_seq", "seq_before >= 0"),
    ck("ck_resvactivation_activated_by", _in("activated_by", ACTIVATION_TRIGGERS)),
)

product_decision_confirmations = Table(
    "product_decision_confirmations", _md,
    _pk(),
    Column("decision_set", String(32), nullable=False),
    Column("version", String(32), nullable=False),
    Column("content_hash", CHAR(64), nullable=False),
    Column("confirmed_by_admin_id", Integer, nullable=False),
    _created(),
    uq("uq_productdecision_set_version_hash", "decision_set", "version", "content_hash"),
)

reservation_resolution_decisions = Table(
    "reservation_resolution_decisions", _md,
    _pk(),
    Column("user_id", Integer, nullable=False),
    Column("username_snapshot", String(128), nullable=False),
    Column("action", String(24), nullable=False),
    Column("target_purchase_id", Integer, nullable=True),
    Column("cap_policy", String(24), nullable=True),
    Column("delivered_by_json", Text, nullable=True),
    Column("reason", Text, nullable=False),
    Column("evidence_json", Text, nullable=False),
    Column("state_json", Text, nullable=False),
    Column("state_hash", CHAR(64), nullable=False),
    Column("actor_admin_id", Integer, nullable=False),
    _created(),
    ck("ck_resvdecision_action", _in("action", RESOLUTION_ACTIONS)),
    ck("ck_resvdecision_cap_policy", "cap_policy IS NULL OR " + _in("cap_policy", CAP_POLICIES)),
    ck("ck_resvdecision_transfer_shape",
       "(action = 'transfer_to_purchase') = (target_purchase_id IS NOT NULL AND cap_policy IS NOT NULL)"),
)

reservation_resolutions = Table(
    "reservation_resolutions", _md,
    _pk(),
    Column("idempotency_key", String(128), nullable=False),
    Column("request_hash", CHAR(64), nullable=False),
    Column("decision_id", _big(), nullable=False),
    Column("user_id", Integer, nullable=False),
    Column("action", String(24), nullable=False),
    Column("target_purchase_id", Integer, nullable=True),
    Column("before_json", Text, nullable=False),
    Column("after_json", Text, nullable=False),
    Column("result_json", Text, nullable=False),
    Column("actor_admin_id", Integer, nullable=False),
    Column("ip", String(64), nullable=True),
    _created(),
    uq("uq_resvresolution_idempotency_key", "idempotency_key"),
    uq("uq_resvresolution_decision", "decision_id"),
    ck("ck_resvresolution_action", _in("action", RESOLUTION_ACTIONS)),
)

RESERVATION_TRANSITION_TABLES = tuple(RESERVATION_TRANSITION_METADATA.sorted_tables)
