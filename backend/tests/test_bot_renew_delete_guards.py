"""Empty/failed renewal never reaches payment; delete retries keep one key."""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.dirname(__file__)]
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _no_network
_no_network.install([])
from app.telegram_bot.handlers import customer_account as account, customer_purchase as purchase
from app.telegram_bot import panel_bridge, remote_bridge


async def run():
    call = SimpleNamespace(from_user=SimpleNamespace(id=123), answer=AsyncMock(),
                           message=SimpleNamespace(edit_text=AsyncMock()))
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
    user = {"username": "u", "connections": []}
    with patch.object(purchase, "_menu_item_enabled", AsyncMock(return_value=True)), \
         patch.object(purchase, "_blocked_from_buying", AsyncMock(return_value=False)), \
         patch.object(purchase, "_clear_state_keep_account", AsyncMock()), \
         patch.object(purchase, "_resolve_account", AsyncMock(return_value=user)), \
         patch.object(purchase, "_start_package_picker", AsyncMock()) as picker:
        for result in ([], panel_bridge.ApiError("unavailable")):
            kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with patch.object(purchase.api, "list_purchases", AsyncMock(**kwargs)):
                await purchase.cb_renew(call, state)
            picker.assert_not_awaited()
        with patch.object(purchase.api, "list_purchases", AsyncMock(return_value=[{"id": 7}])):
            await purchase.cb_renew(call, state)
        picker.assert_awaited_once_with(call, state, "renew")
        assert any(c.kwargs.get("renew_purchase_id") == 7 for c in state.update_data.await_args_list)

    group = {"key": "batch", "deletable": True, "connections": [
        {"id": 8, "purchase_id": 7, "created_at": "2026-10-19T10:00:00"}]}
    callback = SimpleNamespace(key="batch")
    delete = AsyncMock(return_value={"ok": True})
    with patch.object(account, "_resolve_account", AsyncMock(return_value=user)), \
         patch.object(account, "group_connections_by_purchase", return_value=[group]), \
         patch.object(account, "purchases_kb", return_value=None), \
         patch.object(account, "_account_text", return_value="account"), \
         patch.object(account.api, "get_user", AsyncMock(return_value=user)), \
         patch.object(account.api, "delete_purchase", delete):
        await asyncio.gather(account.cb_delete_confirm(call, callback, state),
                             account.cb_delete_confirm(call, callback, state))
        keys = [c.kwargs["idempotency_key"] for c in delete.await_args_list]
        assert len(keys) == 2 and keys[0] == keys[1]
        original = account._delete_key(123, "u", group)
        group["connections"][0]["created_at"] = "2026-10-20T10:00:00"
        assert account._delete_key(123, "u", group) != original, "recreated IDs are not a replay"
        assert account._delete_key(456, "u", group) != original
        group["deletable"] = False
        delete.reset_mock()
        await account.cb_delete_confirm(call, callback, state)
        delete.assert_not_awaited()

    bridge = panel_bridge.PanelBridge()
    with patch.object(panel_bridge, "_call", AsyncMock(return_value={"ok": True})) as invoke:
        await bridge.delete_purchase("u", 7, idempotency_key="stable")
        assert invoke.await_args.kwargs["x_idempotency_key"] == "stable"
        await bridge.delete_connection("u", 8, idempotency_key="stable-conn")
        assert invoke.await_args.kwargs["x_idempotency_key"] == "stable-conn"
    remote = remote_bridge.RemoteBridge("http://fake", "fake")
    with patch.object(remote, "_call", AsyncMock(return_value={"ok": True})) as invoke:
        await remote.delete_purchase("u", 7, idempotency_key="stable")
        assert invoke.await_args.kwargs["extra_headers"] == {"X-Idempotency-Key": "stable"}
        await remote.delete_connection("u", 8, idempotency_key="stable-conn")
        assert invoke.await_args.kwargs["extra_headers"] == {"X-Idempotency-Key": "stable-conn"}


asyncio.run(run())
assert not _no_network.attempts
print("PASS empty/error renewal stopped before payment; stable delete key through both bridges; stale confirmation rejected")
