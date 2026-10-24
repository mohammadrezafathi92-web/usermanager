"""action_type -> implementation, as 'module:function'. Pure data, shared by
the parent (which refuses to spawn for an unregistered action) and the
child (which imports the implementation). Three stored forward actions
and their three stored-identity absence actions (compensation or deletion)
require the child entry's Linux/runtime/identity/lease/contract guards.
No live worker invokes them and other actions remain unregistered. Entries are added here, in code, by the
batch that converts each writer - never supplied by a caller."""
from __future__ import annotations

from .remote_action import ActionType

_SELFTEST = "app.services.remote_runner_selftest"

ACTIONS: dict[ActionType, str] = {
    ActionType.SELFTEST_ECHO: f"{_SELFTEST}:echo",
    ActionType.SELFTEST_SLEEP: f"{_SELFTEST}:sleep",
    ActionType.SELFTEST_CRASH: f"{_SELFTEST}:crash",
    ActionType.SELFTEST_ENVIRONMENT: f"{_SELFTEST}:environment",
    ActionType.WG_ENSURE_PRESENT: "app.services.provisioning_child_entry:forward",
    ActionType.XRAY_ENSURE_PRESENT: "app.services.provisioning_child_entry:forward",
    ActionType.SOFTETHER_ENSURE_PRESENT: "app.services.provisioning_child_entry:forward",
    ActionType.WG_ENSURE_ABSENT: "app.services.provisioning_child_entry:absent",
    ActionType.XRAY_ENSURE_ABSENT: "app.services.provisioning_child_entry:absent",
    ActionType.SOFTETHER_ENSURE_ABSENT: "app.services.provisioning_child_entry:absent",
}

# Legacy writers (10.8): in 'enforced' every one of them runs here, with
# today's semantics (legacy_remote_blocks) under LegacyChildGuard. The six
# ensure_* entries above route their legacy phase to the same entry.
_LEGACY = "app.services.provisioning_child_legacy:run"
ACTIONS.update({action: _LEGACY for action in (
    ActionType.WG_BOUNCE_PEER, ActionType.WG_SET_PEER_ENABLED, ActionType.WG_RENAME_PEER,
    ActionType.WG_SET_SPEED_QUEUE, ActionType.XRAY_BOUNCE_CLIENT, ActionType.XRAY_SET_CLIENT_ENABLED,
    ActionType.XRAY_REBUILD_CLIENTS, ActionType.SOFTETHER_BOUNCE_USER, ActionType.SOFTETHER_SET_USER_ENABLED,
    ActionType.PPP_KICK_SESSION, ActionType.MT_PUSH_RADIUS, ActionType.MT_PUSH_PPTP, ActionType.MT_PUSH_SSTP,
    ActionType.MT_PUSH_L2TP, ActionType.MT_PUSH_IKEV2, ActionType.WG_RECONCILE_INTERFACE_ADDRESS,
    ActionType.WG_REFUND_STOP, ActionType.WG_REFUND_REMOVE)})
# The per-node contract probe (5.10), same fencing in its own 'probe' phase.
ACTIONS[ActionType.CONTRACT_PROBE] = "app.services.provisioning_child_probe:run"
