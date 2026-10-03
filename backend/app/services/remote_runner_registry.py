"""action_type -> implementation, as 'module:function'. Pure data, shared by
the parent (which refuses to spawn for an unregistered action) and the
child (which imports the implementation). Only the runner's self-test
actions exist in batch L1a-1: no real remote mutation is registered, so the
runner cannot touch a node yet. Entries are added here, in code, by the
batch that converts each writer - never supplied by a caller."""
from __future__ import annotations

from .remote_action import ActionType

_SELFTEST = "app.services.remote_runner_selftest"

ACTIONS: dict[ActionType, str] = {
    ActionType.SELFTEST_ECHO: f"{_SELFTEST}:echo",
    ActionType.SELFTEST_SLEEP: f"{_SELFTEST}:sleep",
    ActionType.SELFTEST_CRASH: f"{_SELFTEST}:crash",
    ActionType.SELFTEST_ENVIRONMENT: f"{_SELFTEST}:environment",
}
