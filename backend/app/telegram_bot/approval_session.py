"""The bot's side of receipt-approval registration (Receipt Void design
5.6): before an approval does anything, tell the panel which pending
request is being approved, by whom, and how.

Only the panel decides what that means. While its mode is 'off' the answer
is "carry on as always" and nothing is stored; in 'shadow' the approval is
registered for later comparison and still nothing is gated.

Transient registration failures in off/shadow still use the legacy path.
Explicit fail-closed backend decisions (blocked/required) are different:
they propagate to the caller and must stop the approval, never run legacy
work (design 5.5/5.6).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from . import storage

logger = logging.getLogger(__name__)

# 'approval_registration_refused:...' is every refusal under 'required' (P9a):
# the approval stops there, it never runs the legacy path.
FAIL_CLOSED_CODES = ("receipt_approval_blocked", "required_mode_not_available", "execution_token_not_supported",
                     "auto_approval_rate_limited", "auto_approval_policy_denied", "approval_registration_refused")


# The central policy's refusal reasons (receipt_approval_auto.evaluate) - the
# only part of a refusal detail ever passed on, and only when it is one of these.
POLICY_REASONS = ("kind_not_auto_eligible", "disabled", "outside_window", "over_cap", "no_telegram_identity",
                  "returning_unknown", "not_returning")


class ApprovalBlocked(RuntimeError):
    """A backend fail-closed decision must never fall back to legacy work."""
    def __init__(self, code, retry_after_seconds=None, reason=None):
        super().__init__(code)
        self.retry_after_seconds = retry_after_seconds
        self.reason = reason


def policy_reason(detail: str) -> Optional[str]:
    found = re.search(r"auto_approval_policy_denied:([a-z_]+)", detail or "")
    return found.group(1) if found and found.group(1) in POLICY_REASONS else None

LEGACY = {"mode": "off", "approval_uuid": None, "proceed_legacy": True}
APPROVAL_PROTOCOL_VERSION = 1
REGISTERED_KINDS = ("new", "renew", "topup")       # 'link' is not a payment


def intent_of(pending: dict) -> Optional[dict]:
    """The request body for one pending row, or None for a kind that is
    never registered."""
    kind = pending.get("kind")
    if kind not in REGISTERED_KINDS:
        return None
    price = int(pending.get("price") or 0)
    final_price = pending.get("final_price")
    amount = price if kind == "topup" or final_price is None else int(final_price)
    connections = []
    if kind == "new" and pending.get("node_id"):
        connections = [{"node_id": pending["node_id"], "protocol": pending.get("protocol") or "", "flow": ""}]
    return {
        "pending_source_instance_id": storage.instance_id(), "pending_local_id": int(pending["id"]), "kind": kind,
        "target_username": pending["target_username"], "amount": amount,
        "package_id": pending.get("package_id") if kind != "topup" else None,
        "renew_purchase_id": pending.get("renew_purchase_id") if kind == "renew" else None,
        "payment_card_id": pending.get("payment_card_id"), "receipt_file_id": pending.get("receipt_file_id"),
        "discount_code": pending.get("discount_code") if kind != "topup" else None,
        "referral_code": pending.get("referral_code") if kind == "new" else None,
        "list_price": price, "connections": connections, "telegram_id": pending.get("telegram_id"),
        "owner_admin_id": pending.get("owner_admin_id"),
        # This bot speaks the P9a approval protocol (required registration,
        # one service per renewal, fail-closed refusals).
        "approval_protocol_version": APPROVAL_PROTOCOL_VERSION,
    }


async def begin(pending: dict, *, approved_by_telegram_id: Optional[int] = None, auto: bool = False,
                local_decision: Optional[str] = None) -> dict:
    """Registers the approval. Returns LEGACY for unregistered kinds and
    recoverable off/shadow failures; fail-closed mode decisions raise."""
    try:
        intent = intent_of(pending)
        if intent is None or (not auto and not approved_by_telegram_id):
            return dict(LEGACY)
        from .panel_bridge import api          # late: the bridge is chosen at import time
        if auto:
            intent["local_decision"] = local_decision
            answer = await api.begin_approval(intent, "auto")
        else:
            intent["approved_by_telegram_id"] = int(approved_by_telegram_id)
            answer = await api.begin_approval(intent, "manual")
        if not isinstance(answer, dict):
            if auto:
                raise ApprovalBlocked("auto_approval_unavailable")
            return dict(LEGACY)
        if auto:
            mode = answer.get("mode")
            legacy = answer.get("proceed_legacy")
            registered = isinstance(answer.get("approval_uuid"), str) and bool(answer["approval_uuid"]) \
                and type(answer.get("execution_token")) is int
            valid = (mode == "off" and legacy is True and not answer.get("approval_uuid")) or \
                (mode == "shadow" and (legacy is True or (legacy is False and registered))) or \
                (mode == "required" and legacy is False and registered
                 and isinstance(answer.get("central_decision"), dict))
            if not valid:
                raise ApprovalBlocked("auto_approval_unavailable")
        if answer.get("shadow_error"):
            logger.info("pending request %s: approval not registered (%s)", pending.get("id"), answer["shadow_error"])
        if pending.get("kind") == "renew" and answer.get("renew_purchase_id") and not pending.get("renew_purchase_id"):
            # The panel registered the renewal of the customer's only service:
            # execute exactly that service (never the whole user).
            pending["renew_purchase_id"] = answer["renew_purchase_id"]
        return answer
    except Exception as exc:
        if isinstance(exc, ApprovalBlocked):
            raise
        detail = str(getattr(exc, "detail", "") or exc)
        code = next((candidate for candidate in FAIL_CLOSED_CODES if candidate in detail), None)
        if code:
            # Safe diagnostic codes only; never forward raw HTTP bodies or
            # credentials from a remote error into the receipt message.
            if "auto_approval_rate_limited" in detail:
                payload = getattr(exc, "detail", None)
                retry = payload.get("retry_after_seconds") if isinstance(payload, dict) else None
                raise ApprovalBlocked("auto_approval_rate_limited", retry) from exc
            if "auto_approval_policy_denied" in detail:
                raise ApprovalBlocked("auto_approval_policy_denied", reason=policy_reason(detail)) from exc
            raise ApprovalBlocked(code) from exc
        if auto:
            raise ApprovalBlocked("auto_approval_unavailable") from exc
        logger.warning("pending request %s: approval registration unavailable (%s) - proceeding as before",
                       pending.get("id"), exc)
        return dict(LEGACY)


async def finish(session: dict, ok: bool) -> None:
    """Tells the panel the approval is over: it went through (the panel
    then checks that everything it expected was recorded) or it failed
    before doing anything. Nothing to do for an unregistered approval.
    Never raises - the approval's own outcome is already decided."""
    approval_uuid = (session or {}).get("approval_uuid")
    if not approval_uuid:
        return
    try:
        from .panel_bridge import api
        result = await api.finalize_approval(approval_uuid, failed=not ok)
        if isinstance(result, dict) and ok and result.get("state") != "completed":
            logger.info("approval %s finished but is %s (missing: %s)", approval_uuid, result.get("state"),
                        result.get("missing_effects"))
    except Exception as exc:
        logger.warning("approval %s: could not be finalized (%s)", approval_uuid, exc)
