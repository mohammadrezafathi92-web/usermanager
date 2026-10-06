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
from typing import Optional

from . import storage

logger = logging.getLogger(__name__)

FAIL_CLOSED_CODES = ("receipt_approval_blocked", "required_mode_not_available", "execution_token_not_supported")


class ApprovalBlocked(RuntimeError):
    """A backend fail-closed decision must never fall back to legacy work."""

LEGACY = {"mode": "off", "approval_uuid": None, "proceed_legacy": True}
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
            return dict(LEGACY)
        if answer.get("shadow_error"):
            logger.info("pending request %s: approval not registered (%s)", pending.get("id"), answer["shadow_error"])
        return answer
    except Exception as exc:
        detail = str(getattr(exc, "detail", "") or exc)
        code = next((candidate for candidate in FAIL_CLOSED_CODES if candidate in detail), None)
        if code:
            raise ApprovalBlocked(code) from exc
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
