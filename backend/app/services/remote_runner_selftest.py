"""Self-test actions of the mutation runner (design 10.6: "runner
self-test" is a precondition of 'enforced'; also what the tests drive).
No node, no network, no database. Imported only by the runner child."""
from __future__ import annotations

import os
import sys
import time

from .remote_action import Outcome, RemoteActionDTO, RemoteActionResult


def _touch(path) -> None:
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))


def echo(dto: RemoteActionDTO) -> RemoteActionResult:
    return RemoteActionResult(action_id=dto.action_id, outcome=Outcome.SUCCEEDED,
                              observed={"params": dto.params, "pid": os.getpid()})


def sleep(dto: RemoteActionDTO) -> RemoteActionResult:
    """Stands in for a remote call that hangs. `started_marker` is written
    once the action is running (i.e. after the node lock was taken)."""
    _touch(dto.params.get("started_marker"))
    time.sleep(float(dto.params.get("seconds", 0)))
    return RemoteActionResult(action_id=dto.action_id, outcome=Outcome.SUCCEEDED, observed={"slept": True})


def crash(dto: RemoteActionDTO) -> RemoteActionResult:
    """Dies the way a killed/crashed runner does: no result is written."""
    _touch(dto.params.get("started_marker"))
    os._exit(int(dto.params.get("exit_code", 9)))


def environment(dto: RemoteActionDTO) -> RemoteActionResult:
    """Reports - from INSIDE the child - whether any secret value of this
    DTO is visible in the child's argv or environment."""
    secrets = [str(v) for v in list(dto.node_secrets.values()) + list(dto.credential.values()) if v]
    argv = " ".join(sys.argv)
    env = "\n".join(f"{k}={v}" for k, v in os.environ.items())
    return RemoteActionResult(
        action_id=dto.action_id, outcome=Outcome.SUCCEEDED,
        observed={
            "secret_in_argv": any(s in argv for s in secrets),
            "secret_in_env": any(s in env for s in secrets),
            "argv_length": len(sys.argv),
            "secrets_received": len(secrets),
        },
    )
