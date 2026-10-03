"""Lifecycle/P6 batch L1a-1 - the RemoteActionDTO / RemoteActionResult
contract (design v7.1, section 10.9).

Run:  python3 backend/tests/test_remote_action_contract.py

Covers the contract half of LP-156, LP-159, LP-162, LP-164: nothing that is
not plain data can enter a DTO, secrets never appear in repr/str/redacted,
the wire format is JSON only, and a version/shape mismatch is rejected.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import remote_action as ra
from app.services.remote_action import ActionType, Outcome, RemoteActionDTO, RemoteActionResult

failures: list[str] = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def raises(exc_type, fn):
    try:
        fn()
    except exc_type:
        return True
    except Exception as exc:  # noqa: BLE001
        return f"raised {type(exc).__name__} instead"
    return "did not raise"


SECRET = "S3CR3T-marker-9f2c"
NODE_SECRET = "N0DE-PASSWORD-marker-77aa"


def good(**over):
    kwargs = dict(
        action_type=ActionType.WG_ENSURE_PRESENT, node_id=7, backend="mikrotik_wg",
        node_config={"host": "10.0.0.1", "port": 8728, "interface": "wg0"},
        node_secrets={"password": NODE_SECRET},
        identity={"peer_name": "user-a-1", "allowed": ["10.66.0.2/32"]},
        credential={"xr_uuid": SECRET},
        params={"speed_limit_mbps": 10, "enabled": True, "ratio": 1.5, "note": None},
        timeouts={"connect_timeout": 5, "read_timeout": 10, "hard_deadline": 120},
        contract={"not_exist": []},
        fencing={"installation_uuid": str(uuid.uuid4()), "ownership_epoch": 3},
    )
    kwargs.update(over)
    return RemoteActionDTO(**kwargs)


print("--- DTO: only plain data gets in (LP-156, LP-164) ---")
dto = good()
check("a well-formed DTO is accepted", dto.action_type, ActionType.WG_ENSURE_PRESENT)
check("action_type may be given as its string value", good(action_type="xray_ensure_present").action_type,
      ActionType.XRAY_ENSURE_PRESENT)
check("action_id defaults to a canonical uuid", str(uuid.UUID(dto.action_id)), dto.action_id)

engine = create_engine("sqlite://")
models.Base.metadata.create_all(engine)
session = sessionmaker(bind=engine)()
orm_node = models.Node(name="n", type="mikrotik")
bad_values = {
    "a SQLAlchemy Session": session,
    "an ORM object": orm_node,
    "a Query": session.query(models.Node),
    "a function": good,
    "a lambda (closure)": (lambda: SECRET),
    "a bound method": session.commit,
    "bytes": b"raw",
    "a set": {1, 2},
    "an arbitrary object": object(),
    "a class": dict,
    "NaN": float("nan"),
    "infinity": float("inf"),
}
for label, value in bad_values.items():
    for field_name in ("params", "identity", "credential", "node_config", "node_secrets", "contract", "fencing"):
        check(f"{label} directly in {field_name} is rejected",
              raises(ra.NotSerializable, lambda: good(**{field_name: {"x": value}})))
    check(f"{label} nested three levels deep is rejected",
          raises(ra.NotSerializable, lambda: good(params={"a": [{"b": [1, value]}]})))
check("an ORM object as the field itself is rejected", raises(ra.NotSerializable, lambda: good(params=orm_node)))
check("a non-string dict key is rejected", raises(ra.NotSerializable, lambda: good(params={1: "x"})))
check("NotSerializable is a TypeError (and a RemoteActionError)",
      (issubclass(ra.NotSerializable, TypeError), issubclass(ra.NotSerializable, ra.RemoteActionError)), (True, True))
check("an unknown action_type is rejected", raises(ra.RemoteActionError, lambda: good(action_type="rm_rf")))
check("a callable as action_type is rejected", raises(ra.RemoteActionError, lambda: good(action_type=good)))
check("node_id 0 / negative / bool / str is rejected",
      [raises(ra.RemoteActionError, lambda v=v: good(node_id=v)) for v in (0, -1, True, "7")], [True] * 4)
check("a non-canonical action_id is rejected", raises(ra.RemoteActionError, lambda: good(action_id="abc")))
check("a non-positive timeout is rejected", raises(ra.RemoteActionError, lambda: good(timeouts={"hard_deadline": 0})))
check("a bool timeout is rejected", raises(ra.RemoteActionError, lambda: good(timeouts={"hard_deadline": True})))
session.close()

print("--- DTO: detached from the caller's objects ---")
caller_params = {"list": [1, 2]}
detached = good(params=caller_params)
caller_params["list"].append(3)
caller_params["new"] = "x"
check("mutating the caller's dict afterwards does not change the DTO", detached.params, {"list": [1, 2]})
check("the DTO is frozen", raises(Exception, lambda: setattr(detached, "node_id", 9)))

print("--- DTO: secrets never show (LP-159) ---")
for label, text in (("repr", repr(dto)), ("str", str(dto)), ("format", f"{dto}"),
                    ("redacted() as JSON", json.dumps(dto.redacted()))):
    check(f"{label} does not contain the customer credential", SECRET in text, False)
    check(f"{label} does not contain the node secret", NODE_SECRET in text, False)
check("redacted() keeps the KEYS of the secret fields",
      (dto.redacted()["credential"], dto.redacted()["node_secrets"]),
      ({"xr_uuid": ra.REDACTED}, {"password": ra.REDACTED}))
check("redacted() keeps non-secret fields intact", dto.redacted()["identity"], dto.identity)
check("only to_wire() carries the secrets", (SECRET in dto.to_wire(), NODE_SECRET in dto.to_wire()), (True, True))
try:
    raise RuntimeError(f"failed while handling {dto}")
except RuntimeError as exc:
    check("an exception message built from the DTO carries no secret", SECRET in str(exc) or NODE_SECRET in str(exc), False)

print("--- DTO: the wire is JSON, versioned (LP-162, LP-164) ---")
wire = dto.to_wire()
check("the wire is a JSON object", isinstance(json.loads(wire), dict))
check("round trip preserves everything", RemoteActionDTO.from_wire(wire), dto)
check("the wire does not look like a pickle", wire.lstrip()[0], "{")
check("a different schema_version is rejected",
      raises(ra.SchemaVersionMismatch, lambda: RemoteActionDTO.from_wire(
          json.dumps({**json.loads(wire), "schema_version": ra.SCHEMA_VERSION + 1}))))
check("a missing schema_version is rejected",
      raises(ra.SchemaVersionMismatch, lambda: RemoteActionDTO.from_wire(
          json.dumps({k: v for k, v in json.loads(wire).items() if k != "schema_version"}))))
check("an unknown field is rejected",
      raises(ra.RemoteActionError, lambda: RemoteActionDTO.from_wire(
          json.dumps({**json.loads(wire), "callback": "os.system"}))))
check("non-JSON is rejected", raises(ra.RemoteActionError, lambda: RemoteActionDTO.from_wire("\x80\x04pickle")))
check("a JSON array is rejected", raises(ra.RemoteActionError, lambda: RemoteActionDTO.from_wire("[1]")))

print("--- result: eight outcomes, unknown is never success or failure ---")
check("exactly the eight outcomes of the design",
      sorted(o.value for o in Outcome),
      sorted(["succeeded", "already_present_verified", "absent_verified", "conflict", "unreadable",
              "transport_error", "timeout_unknown", "killed_unknown"]))
aid = str(uuid.uuid4())
result = RemoteActionResult(action_id=aid, outcome="absent_verified", remote_outcome="verified_absent",
                            observed={"peer": None}, duration_ms=12)
check("round trip preserves a result", RemoteActionResult.from_wire(result.to_wire()), result)
check("an unknown outcome is rejected", raises(ra.RemoteActionError, lambda: RemoteActionResult(action_id=aid, outcome="ok")))
check("an unknown remote_outcome is rejected",
      raises(ra.RemoteActionError, lambda: RemoteActionResult(action_id=aid, outcome="succeeded", remote_outcome="gone")))
check("a non-bool write_attempted is rejected",
      raises(ra.RemoteActionError, lambda: RemoteActionResult(action_id=aid, outcome="succeeded", write_attempted=1)))
check("a non-plain observed value is rejected",
      raises(ra.NotSerializable, lambda: RemoteActionResult(action_id=aid, outcome="succeeded", observed={"x": object()})))
check("a result of another schema_version is rejected",
      raises(ra.SchemaVersionMismatch, lambda: RemoteActionResult.from_wire(
          json.dumps({**json.loads(result.to_wire()), "schema_version": 99}))))
unknown_expected = {
    ("succeeded", False): False, ("already_present_verified", False): False, ("absent_verified", False): False,
    ("conflict", False): False, ("unreadable", False): False,
    ("transport_error", False): False, ("transport_error", True): True,
    ("timeout_unknown", False): True, ("timeout_unknown", True): True,
    ("killed_unknown", True): True,
}
check("is_unknown: timeout/killed always, transport_error only after a write was attempted",
      {key: RemoteActionResult(action_id=aid, outcome=key[0], write_attempted=key[1]).is_unknown
       for key in unknown_expected},
      unknown_expected)
killed = RemoteActionResult.killed_unknown(aid, "hard_deadline_exceeded", 5)
check("killed_unknown() is unknown and assumes a write may have happened",
      (killed.outcome, killed.is_unknown, killed.write_attempted), (Outcome.KILLED_UNKNOWN, True, True))

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
