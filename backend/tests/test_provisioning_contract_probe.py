"""Node contract bootstrap probe and verify (Lifecycle 5.10).

Child probe (in-process, runner role simulated, guarded child clients over
fake transports - no node, no network):
  * MikroTik: a namespaced test peer is created, read back as an exact
    match, removed and read absent; the version-based server fingerprint;
  * Marzban: the not_exist matchers are built ONLY from two equal answers
    (read before/after; delete of the absent user twice) and pass the
    contract schema; unequal answers, an HTML/non-JSON answer, or a panel
    that renames the user give 'not ready' with no matcher, the last one
    reporting probe_cleanup_unknown and the test identity (no uuid);
Parent verify:
  * refused unless the gate is 'enforced';
  * a successful probe writes exactly one 'ready' row with the current
    adapter version and configuration fingerprint; a node edited during the
    probe is refused;
  * invalidate_stale turns a ready row whose node configuration, backend or
    adapter version changed into 'invalidated' with the reason.
"""
import contextlib
import datetime as dt
import json
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-probe-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
os.environ["UM_LOCK_DIR"] = os.path.join(DIRECTORY, "locks")
os.makedirs(os.environ["UM_LOCK_DIR"], mode=0o700)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

import requests
from fastapi import HTTPException

from app import models, models_provisioning as mp
from app.database import SessionLocal, engine
from app.services import gate_locks, legacy_remote, node_gate, provisioning_child_authority as authority
from app.services import provisioning_child_entry as entry, provisioning_child_probe as probe
from app.services import provisioning_contracts as contracts, provisioning_control as control
from app.services import provisioning_host, provisioning_schema, remote_runner
from app.services.provisioning_host import HostIdentity
from app.services.remote_action import Outcome, RemoteActionResult

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


models.Base.metadata.create_all(engine)
check("provisioning schema ready", provisioning_schema.bootstrap(engine, SessionLocal)["ready"])
IDENTITY = HostIdentity("c3" * 32, str(uuid.uuid4()))
with SessionLocal() as db:
    db.add_all([
        models.AdminUser(id=1, username="root", hashed_password="x", is_superadmin=True),
        models.Node(id=61, name="mt", type=models.NodeType.mikrotik, mt_host="10.6.6.6", mt_username="a",
                    mt_password="p", mt_wireguard_interface="wg0"),
        models.Node(id=62, name="mz", type=models.NodeType.xray, xr_panel_mode="marzban",
                    xr_panel_base_url="http://10.6.6.7:8000", xr_panel_username="u", xr_panel_password="p",
                    xr_inbound_tag="VLESS")])
    row = db.get(mp.ProvisioningRuntimeState, 1)
    INSTALLATION = row.installation_uuid
    now = dt.datetime.utcnow()
    row.gate_mode, row.gate_mode_epoch, row.lock_backend, row.lock_verified_at = "enforced", 1, "flock", now
    row.owner_state, row.owner_host_id, row.owner_boot_id = "active", IDENTITY.host_id, IDENTITY.boot_id
    row.owner_claimed_at = row.owner_heartbeat_at = now
    row.ownership_epoch = 1
    db.commit()
with open(os.path.join(os.environ["UM_LOCK_DIR"], "installation.id"), "w") as fh:
    fh.write(INSTALLATION + "\n")
os.chmod(os.path.join(os.environ["UM_LOCK_DIR"], "installation.id"), 0o600)
gate_locks.write_mode_state(INSTALLATION, "1")
node_gate.reset_cache()


def node(node_id):
    with SessionLocal() as db:
        value = db.get(models.Node, node_id)
        db.expunge(value)
        return value


# ------------------------------------------------------------------ fake RouterOS (stateful)
class FakeRouteros:
    def __init__(self):
        self.peers, self.next_id = {}, 1

    def __call__(self, command, **kwargs):
        if command == "/system/resource/print":
            yield {"version": "7.14.2 (stable)", "total-memory": "1"}
        elif command == "/interface/wireguard/peers/print":
            for peer_id, peer in self.peers.items():
                yield dict(peer, **{".id": peer_id})
        elif command == "/interface/wireguard/peers/add":
            peer_id = f"*{self.next_id}"
            self.next_id += 1
            self.peers[peer_id] = {"interface": kwargs.get("interface"), "public-key": kwargs.get("public-key"),
                                   "allowed-address": kwargs.get("allowed-address"), "comment": kwargs.get("comment")}
            yield {"ret": peer_id}
        elif command == "/interface/wireguard/peers/remove":
            self.peers.pop(kwargs.get(".id"), None)
            yield {}
        elif command == "/queue/simple/print":
            return
        else:
            yield {}

    def rawCmd(self, command, *words):  # noqa: N802 - queries (select/where): no queue exists here
        return
        yield

    def close(self):
        pass


# ------------------------------------------------------------------ fake Marzban (HTTP adapter level)
class FakeMarzban:
    def __init__(self, *, not_found=({"detail": "User not found"},), rename=False, html=False):
        self.users, self.not_found, self.rename, self.html, self.calls = {}, list(not_found), rename, html, 0

    def _nf(self):
        self.calls += 1
        return self.not_found[(self.calls - 1) % len(self.not_found)]

    def respond(self, request):
        path = requests.utils.urlparse(request.url).path
        method = request.method
        if path == "/api/admin/token":
            return 200, {"access_token": "t"}
        if path.startswith("/api/user/"):
            name = path.rsplit("/", 1)[1]
            if method == "GET":
                if self.html:
                    return 404, "<html>not found</html>"
                return (200, self.users[name]) if name in self.users else (404, self._nf())
            if method == "DELETE":
                return (200, self.users.pop(name) and {}) if name in self.users else (404, self._nf())
        if path == "/api/user" and method == "POST":
            body = json.loads(request.body)
            stored = dict(body, username=body["username"].upper() if self.rename else body["username"])
            self.users[stored["username"]] = stored
            return 200, stored
        return 404, {"detail": "Not Found"}


def http_patch(panel):
    def send(self, request, **kwargs):
        status, body = panel.respond(request)
        response = requests.Response()
        response.status_code, response.url, response.request = status, request.url, request
        if isinstance(body, str):
            response._content, response.headers["Content-Type"] = body.encode(), "text/html"
        else:
            response._content, response.headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        return response
    return patch.object(requests.adapters.HTTPAdapter, "send", send)


def probe_dto(node_id):
    captured = []
    with patch.object(provisioning_host, "read_identity", return_value=IDENTITY), \
            patch.object(remote_runner, "run_action", side_effect=lambda d: captured.append(d) or RemoteActionResult(
                action_id=d.action_id, outcome=Outcome.SUCCEEDED, observed={})):
        legacy_remote.dispatch_probe(node(node_id))
    return captured[-1]


def child_probe(dto, transport):
    mode_lock = gate_locks.FileLock(gate_locks.mode_lock_path(INSTALLATION)).acquire(shared=True, timeout=0)
    node_lock = gate_locks.FileLock(gate_locks.node_lock_path(INSTALLATION, dto.node_id)).acquire(shared=False, timeout=0)
    try:
        with patch.object(authority, "_protected_parent", return_value=True), authority._runner_context(os.getppid()), \
                patch.object(provisioning_host, "read_identity", return_value=IDENTITY), transport:
            with entry.locked_context(mode_lock, node_lock):
                try:
                    return probe.run(dto)
                except Exception as exc:  # noqa: BLE001 - what the real child main reports
                    return RemoteActionResult(action_id=dto.action_id, outcome=Outcome.TRANSPORT_ERROR,
                                              write_attempted=True, error_code="action_raised",
                                              error_sanitized=type(exc).__name__)
    finally:
        node_lock.release()
        mode_lock.release()


# ------------------------------------------------------------------ child: MikroTik
router = FakeRouteros()
result = child_probe(probe_dto(61), patch("librouteros.connect", return_value=router))
observed = result.observed
check("mikrotik probe: ready, no matchers needed, test peer gone, version fingerprint",
      (observed["ready"], observed["contract"], router.peers, len(observed["server_fingerprint"] or "")),
      (True, {"not_exist": []}, {}, 64))

# ------------------------------------------------------------------ child: Marzban
panel = FakeMarzban()
observed = child_probe(probe_dto(62), http_patch(panel)).observed
matchers = observed["contract"]["not_exist"] if observed["contract"] else None
check("marzban probe: ready with read and delete matchers built from equal observations",
      (observed["ready"], sorted((m["op"], m["status"], m["json_path"], m["equals"]) for m in matchers or []), panel.users),
      (True, [("delete", 404, "detail", "User not found"), ("read", 404, "detail", "User not found")], {}))
check("the contract passes the contract schema", contracts.validate_contract(json.dumps(observed["contract"]), "marzban")
      == observed["contract"])
check("no uuid of the test identity is reported", "uuid" not in json.dumps(observed).lower() or
      all(len(v) != 36 for v in observed.values() if isinstance(v, str)))

observed = child_probe(probe_dto(62), http_patch(FakeMarzban(not_found=({"detail": "User not found"},
                                                                         {"detail": "No such user"})))).observed
check("unequal 'not there' answers: not ready, no matcher", (observed["ready"], observed["contract"]), (False, None))
observed = child_probe(probe_dto(62), http_patch(FakeMarzban(html=True))).observed
check("an HTML answer: not ready, nothing created", (observed["ready"], observed["cleanup"]), (False, "clean"))
renaming = FakeMarzban(rename=True)
observed = child_probe(probe_dto(62), http_patch(renaming)).observed
check("a panel that renames the user: not ready, cleanup unknown, the test identity named for manual removal",
      (observed["ready"], observed["cleanup"], observed["reason"], (observed["probe_identity"] or "").startswith("um-probe-")),
      (False, "unknown", "read_after_create_not_match", True))

# ------------------------------------------------------------------ parent verify
with SessionLocal() as db:
    db.get(mp.ProvisioningRuntimeState, 1).gate_mode = "shadow"
    db.commit()
try:
    control.verify_node_contract(SessionLocal, 1, 61)
    refused = "ran"
except HTTPException as exc:
    refused = exc.detail
check("verify is refused unless the gate is enforced", refused, "gate_not_enforced")
with SessionLocal() as db:
    db.get(mp.ProvisioningRuntimeState, 1).gate_mode = "enforced"
    db.commit()
node_gate.reset_cache()


def in_process_runner(transport):
    return lambda dto: child_probe(dto, transport)


with patch.object(provisioning_host, "read_identity", return_value=IDENTITY), \
        patch.object(remote_runner, "run_action", side_effect=in_process_runner(patch("librouteros.connect",
                                                                                      return_value=FakeRouteros()))):
    response = control.verify_node_contract(SessionLocal, 1, 61)
with SessionLocal() as db:
    row = db.query(mp.ProvisioningNodeContract).filter_by(node_id=61).one()
    current = contracts.config_fingerprint(db.get(models.Node, 61))
    check("a successful verify writes one ready row with the current adapter version and configuration",
          (response["ready"], row.state, row.backend, row.adapter_version == contracts.adapter_version(),
           row.config_fingerprint == current, row.verified_by_admin_id),
          (True, "ready", "mikrotik_wg", True, True, 1))


def edit_during(dto):
    with SessionLocal() as db:
        db.get(models.Node, 62).xr_inbound_tag = "CHANGED"
        db.commit()
    return child_probe(dto, http_patch(FakeMarzban()))


with patch.object(provisioning_host, "read_identity", return_value=IDENTITY), \
        patch.object(remote_runner, "run_action", side_effect=edit_during):
    try:
        outcome = control.verify_node_contract(SessionLocal, 1, 62)["ready"]
    except HTTPException as exc:
        outcome = exc.detail
with SessionLocal() as db:
    # The child's own fence (pinned endpoint) refuses before any probe write;
    # record_verified refuses a change that lands after the probe.
    check("a node edited during the probe is refused, no row written",
          (outcome, db.query(mp.ProvisioningNodeContract).filter_by(node_id=62).count()), (False, 0))
with SessionLocal() as db:
    try:
        contracts.record_verified(db, db.get(models.Node, 62), "marzban", contract={"not_exist": []},
                                  server_fingerprint="0" * 64, endpoint_at_dispatch="f" * 64, actor_admin_id=1)
        late = "written"
    except HTTPException as exc:
        late = exc.detail
    db.rollback()
check("an endpoint changed after the probe returned is refused at write time", late, "node_config_changed_during_probe")

# ------------------------------------------------------------------ invalidation
with SessionLocal() as db:
    db.get(models.Node, 61).mt_host = "10.6.6.99"
    db.commit()
    changed = contracts.invalidate_stale(db)
    db.commit()
    row = db.query(mp.ProvisioningNodeContract).filter_by(node_id=61).one()
    check("invalidate_stale: a changed node configuration invalidates its ready row",
          (changed, row.state, row.invalidation_reason, row.invalidated_at is not None),
          ([(61, "mikrotik_wg", "config_changed")], "invalidated", "config_changed", True))
    check("an invalidated row is not touched again", contracts.invalidate_stale(db), [])

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
