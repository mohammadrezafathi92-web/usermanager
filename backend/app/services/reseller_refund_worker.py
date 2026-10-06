"""Opt-in single-host SQLite/WireGuard unpaid-service cancellation.

Other transports require their own final-disconnect/usage contracts and
are refused before any remote mutation. No public payload supplies usage.
"""
import sys
import re
from datetime import datetime

from fastapi import HTTPException

from .. import models
from ..models_reseller_refund import ResellerRefundOperation as Operation, ResellerRefundStep as Step
from ..models_reseller_refund import ResellerSaleBasis as Basis
from . import adapter_mikrotik_wg as wg
from .adapter_base import AdapterError
from .mikrotik_client import MikrotikClient
from .reseller_refund import preview
from .reseller_refund_fence import barrier, enabled
from .reseller_refund_settlement import prepare, capture_counters, verify_stopped, settle


def availability(db, purchase):
    if not enabled():
        return "reseller_cancellation_disabled"
    if not sys.platform.startswith("linux") or db.bind.dialect.name != "sqlite":
        return "reseller_cancellation_platform_unsupported"
    panel = db.query(models.PanelSettings).first()
    if panel and panel.ha_enabled:
        return "reseller_cancellation_ha_unsupported"
    connections = list(purchase.connections)
    if not connections or any(c.type != models.ConnectionType.wireguard for c in connections):
        return "reseller_cancellation_protocol_unsupported"
    # Legacy rewards lack reversible provenance until the loyalty cutover.
    if purchase.user.referral_reward_granted or purchase.user.loyalty_rewards_given:
        return "reseller_cancellation_rewards_untracked"
    basis = db.query(Basis).filter_by(purchase_id=purchase.id).one_or_none()
    sale = db.get(models.LedgerEntry, basis.sale_entry_id) if basis and basis.sale_entry_id else None
    if sale is None or sale.kind != "sale_new" or sale.user_id != purchase.user_id:
        return "reseller_sale_unproven"
    if sale.payment_method is not None or sale.approval_uuid is not None:
        return "reseller_receipt_requires_full_void"
    if sale.discount_code or sale.discount_amount:
        return "reseller_discount_requires_full_void"
    if sale.voided_at is not None:
        return "reseller_sale_already_cancelled"
    return None


def _identity(connection):
    return wg.WireguardIdentity(connection.node.mt_wireguard_interface, connection.wg_peer_name,
                                connection.wg_public_key, connection.wg_client_address)


def _uptime_seconds(value):
    """Strict RouterOS durations; unknown output cannot prove continuity."""
    if not isinstance(value, str):
        raise AdapterError("wg_uptime_unverified")
    match = re.fullmatch(r"(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value)
    if match and any(part is not None for part in match.groups()):
        return sum(int(part or 0) * unit for part, unit in zip(match.groups(), (604800, 86400, 3600, 60, 1)))
    match = re.fullmatch(r"(?:(\d+)w)?(?:(\d+)d)?(\d+):(\d{2}):(\d{2})", value)
    if match and int(match[4]) < 60 and int(match[5]) < 60:
        return sum(int(part or 0) * unit for part, unit in zip(match.groups(), (604800, 86400, 3600, 60, 1)))
    raise AdapterError("wg_uptime_unverified")


def _prove_no_reboot(mt, connection, basis):
    # A reboot followed by enough traffic can overtake the old counter;
    # checking only for a decreased counter misses that lost consumption.
    source_time = min(basis.created_at, connection.created_at)
    elapsed = max(0, (datetime.utcnow() - source_time).total_seconds())
    if _uptime_seconds(mt.get_system_resources().get("uptime")) <= elapsed + 2:
        raise AdapterError("wg_usage_continuity_unverified")


def execute(db, *, purchase_id, actor):
    with barrier(exclusive=True):
        existing = db.query(Operation).filter_by(purchase_id=purchase_id).one_or_none()
        # prepare() performs scope checks even when the Purchase is gone.
        if existing and existing.state == "completed":
            op_id = prepare(db, purchase_id=purchase_id, actor=actor)
            return settle(db, operation_id=op_id)
        purchase = db.get(models.Purchase, purchase_id)
        if purchase is None:
            raise HTTPException(404, "service_not_found")
        reason = availability(db, purchase)
        if reason:
            raise HTTPException(409, reason)
        preview(db, purchase)
        op_id = prepare(db, purchase_id=purchase_id, actor=actor)
        connections = list(purchase.connections)
        basis = db.query(Basis).filter_by(purchase_id=purchase_id).one()
        # Validate ALL identities before disabling any peer.
        try:
            for connection in connections:
                if not all((connection.wg_peer_name, connection.wg_public_key, connection.wg_client_address)):
                    raise HTTPException(409, "refund_resource_changed")
                with MikrotikClient.for_node(connection.node) as mt:
                    found = wg.read(mt, _identity(connection))
                    step = db.query(Step).filter_by(operation_id=op_id, connection_id=connection.id).one()
                    if found.state is wg.ReadState.ABSENT and step.captured_at is not None:
                        continue
                    if found.state is not wg.ReadState.PRESENT_MATCH:
                        raise AdapterError("wg_refund_identity_unverified")
                    _prove_no_reboot(mt, connection, basis)
            for connection in connections:
                step = db.query(Step).filter_by(operation_id=op_id, connection_id=connection.id).one()
                prior = (wg.StoppedUsage(step.final_download_counter, step.final_upload_counter)
                         if step.captured_at is not None else None)
                def save(usage):
                    _prove_no_reboot(mt, connection, basis)
                    capture_counters(db, operation_id=op_id, connection_id=connection.id,
                                     download_bytes=usage.download_bytes, upload_bytes=usage.upload_bytes)
                    return usage
                # End the DB read transaction before entering the network client.
                db.rollback()
                with MikrotikClient.for_node(connection.node) as mt:
                    wg.stop_capture_remove(mt, _identity(connection), persist_usage=save,
                                           previously_captured=prior)
                db.expire_all()
                step = db.query(Step).filter_by(operation_id=op_id, connection_id=connection.id).one()
                verify_stopped(db, operation_id=op_id, connection_id=connection.id,
                               final_total_bytes=step.final_total_bytes)
            return settle(db, operation_id=op_id)
        except AdapterError as exc:
            db.rollback()
            raise HTTPException(409, {"code": "reseller_cancellation_pending",
                                      "operation_id": op_id, "reason": exc.code}) from None
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise HTTPException(503, {"code": "reseller_cancellation_pending",
                                     "operation_id": op_id, "reason": "node_or_database_unavailable"}) from None
