"""Bot local and remote history never override required central decisions."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
from app.services import auto_approve
from app.telegram_bot import approval_session, panel_bridge, storage
from app.telegram_bot.handlers import admin_pending

checks = 0


def check(label, actual, expected=True):
    global checks
    assert actual == expected, f"{label}: {actual!r} != {expected!r}"
    checks += 1
    print("PASS", label)


async def scenario():
    pending = {"id": 1, "kind": "new", "target_username": "buyer", "price": 1000, "telegram_id": 10}
    required = {"mode": "required", "approval_uuid": "fixture", "central_decision": {"central_allowed": True}}
    with patch.object(auto_approve, "decide", return_value=(False, "no local history")), \
            patch.object(panel_bridge.api, "get_approval_mode", new=AsyncMock(return_value={"mode": "required"})) as mode, \
            patch.object(approval_session, "begin", new=AsyncMock(return_value=required)) as register, \
            patch.object(storage, "claim_pending", return_value=True) as claim, \
            patch.object(admin_pending, "perform_approval", new=AsyncMock(return_value=(True, "done"))) as perform, \
            patch.object(auto_approve, "_notify_admins", new=AsyncMock()):
        check("required uses central history despite empty bot history", (await auto_approve.try_auto_approve(pending, object()))[0])
        check("local diagnostic sent once", register.await_args.kwargs["local_decision"], "denied")
        check("one claim", claim.call_count, 1)
        check("reuse same grant no second registration", perform.await_args.kwargs["registered_session"], required)
        mode.return_value = {"mode": "shadow"}
        check("shadow retains local policy", (await auto_approve.try_auto_approve(pending, object()))[0], False)
        check("shadow denial not claimed", claim.call_count, 1)
        check("shadow local denial consumes no grant", register.await_count, 1)
        mode.return_value = {"mode": "required"}
        register.return_value = {"mode": "required", "central_decision": {"central_allowed": False}}
        check("required policy false never executes", (await auto_approve.try_auto_approve(pending, object()))[0], False)
        check("policy denied not claimed", claim.call_count, 1)
        register.return_value = {"mode": "required", "central_decision": {"central_allowed": False,
                                                                          "central_reason": "not_returning"}}
        answer = await auto_approve.try_auto_approve(pending, object())
        check("central refusal names its real reason (no previous paid purchase)",
              (answer[0], "خرید پرداخت‌شدهٔ قبلی ندارد" in answer[1], "بررسی دستی" in answer[1]), (False, True, True))
        register.side_effect = approval_session.ApprovalBlocked("auto_approval_policy_denied", reason="not_returning")
        answer = await auto_approve.try_auto_approve(pending, object())
        check("403 policy refusal names its reason too", "خرید پرداخت‌شدهٔ قبلی ندارد" in answer[1])
        register.side_effect = approval_session.ApprovalBlocked("auto_approval_policy_denied")
        answer = await auto_approve.try_auto_approve(pending, object())
        check("an unknown reason stays the generic sentence", "شرایط تأیید خودکار مرکزی برقرار نیست" in answer[1])
        check("refusals are never claimed or executed", (claim.call_count, perform.await_count), (1, 1))
        register.side_effect = approval_session.ApprovalBlocked("auto_approval_rate_limited", 61)
        answer = await auto_approve.try_auto_approve(pending, object())
        check("rate-limited retained for manual", answer[0], False)
        check("remaining time explanatory", "2 دقیقه" in answer[1])
        check("429 never claimed rejected or executed", (claim.call_count, perform.await_count), (1, 1))

    with patch.object(storage, "instance_id", return_value="fixture"), \
            patch.object(panel_bridge.api, "begin_approval", new=AsyncMock()) as request:
        for failure in [panel_bridge.ApiError({"error": "approval_registration_refused:auto_approval_rate_limited",
                                             "retry_after_seconds": 61}, status_code=429),
                        panel_bridge.ApiError("approval_registration_refused:auto_approval_policy_denied", status_code=403),
                        panel_bridge.ApiError("offline")]:
            request.side_effect = failure
            try:
                await approval_session.begin(pending, auto=True)
                raise AssertionError("auto fell back to legacy")
            except approval_session.ApprovalBlocked as exc:
                check("bridge error blocks auto", str(exc).startswith("auto_approval_"))
        for detail, reason in (("approval_registration_refused:auto_approval_policy_denied:not_returning", "not_returning"),
                               ("approval_registration_refused:auto_approval_policy_denied:<b>x</b> token=abc", None),
                               ("approval_registration_refused:auto_approval_policy_denied", None)):
            request.side_effect = panel_bridge.ApiError(detail, status_code=403)
            try:
                await approval_session.begin(pending, auto=True)
                raise AssertionError("auto fell back to legacy")
            except approval_session.ApprovalBlocked as exc:
                check(f"only a known policy reason is passed on ({reason})", (str(exc), exc.reason),
                      ("auto_approval_policy_denied", reason))
        request.side_effect = None
        for answer in (None, {}, {"mode": "unexpected"}, {"mode": "required", "proceed_legacy": True},
                       {"mode": "shadow", "proceed_legacy": False}):
            request.return_value = answer
            try:
                await approval_session.begin(pending, auto=True)
                raise AssertionError("malformed auto response fell back")
            except approval_session.ApprovalBlocked:
                check("malformed central response blocks auto", True)
        request.return_value = {"mode": "off", "proceed_legacy": True, "approval_uuid": None}
        check("explicit legacy mode remains compatible", (await approval_session.begin(pending, auto=True))["mode"], "off")


asyncio.run(scenario())
print(f"all {checks} checks passed")
