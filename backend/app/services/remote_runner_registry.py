"""action_type -> implementation, as 'module:function'. Pure data, shared by
the parent (which refuses to spawn for an unregistered action) and the
child (which imports the implementation). Three stored forward actions
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
}
