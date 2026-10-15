"""WireGuard interface-address reconciliation (Lifecycle 10.5, 9.1).

Drives the real job through the gated legacy path (gate 'off' here) with a
fake RouterOS client and checks each row of the 10.5 table, the stored
result row, and that nothing runs while every operation type is legacy.
"""
import os
import sys
import tempfile

DIRECTORY = tempfile.mkdtemp(prefix="um-wg-reconcile-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
os.environ["UM_LOCK_DIR"] = os.path.join(DIRECTORY, "locks")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from app import models, models_provisioning as mp
from app.database import SessionLocal, engine
from app.services import mikrotik_client, provisioning_schema, wireguard_reconciliation as job

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


class FakeRouter:
    def __init__(self, addresses, fail=False):
        self.addresses, self.fail, self.writes = list(addresses), fail, []

    def __enter__(self):
        if self.fail:
            raise mikrotik_client.MikrotikError("اتصال به میکروتیک برقرار نشد")
        return self

    def __exit__(self, *exc):
        pass

    def list_interface_addresses(self, interface):
        return [{"address": address, "interface": interface} for address in self.addresses]

    def ensure_interface_address(self, interface, address):
        self.writes.append(("add", address))
        self.addresses = [address]

    def set_interface_address(self, interface, address):
        self.writes.append(("replace", address))
        self.addresses = [address]


models.Base.metadata.create_all(engine)
check("provisioning schema ready", provisioning_schema.bootstrap(engine, SessionLocal)["ready"])
with SessionLocal() as db:
    db.add(models.Node(id=41, name="mt", type=models.NodeType.mikrotik, mt_host="10.5.5.5",
                       mt_wireguard_interface="wg0", mt_client_subnet="10.66.66.0/23"))
    db.add(models.Node(id=42, name="off", type=models.NodeType.mikrotik, enabled=False, mt_wireguard_interface="wg0"))
    db.commit()

router = {"value": None}
mikrotik_client.MikrotikClient.for_node = classmethod(lambda cls, node: router["value"])


def stored():
    with SessionLocal() as db:
        row = db.get(mp.ProvisioningNodeReconciliation, 41)
        return None if row is None else (row.result, row.db_subnet, row.router_address, row.consecutive_failures)


router["value"] = FakeRouter([])
check("nothing runs while every operation type is legacy", (job.run_once(), router["value"].writes), ([], []))
with SessionLocal() as db:
    db.get(mp.ProvisioningTypeMode, "add_connection").mode = "durable"
    db.commit()

cases = (
    ("router without address -> repaired (added)", [], [("add", "10.66.66.1/23")], "repaired", "10.66.66.1/23"),
    ("router narrower than DB -> repaired (widened)", ["10.66.66.1/24"], [("replace", "10.66.66.1/23")], "repaired",
     "10.66.66.1/23"),
    ("router equal to DB -> in_sync", ["10.66.66.1/23"], [], "in_sync", "10.66.66.1/23"),
    ("router wider than DB -> router_wider, never narrowed", ["10.66.64.1/22"], [], "router_wider", "10.66.64.1/22"),
    ("unrelated network -> conflict, nothing changed", ["192.168.9.1/24"], [], "conflict", "192.168.9.1/24"),
)
for label, addresses, writes, result, router_address in cases:
    router["value"] = FakeRouter(addresses)
    outcome = job.run_once()
    check(label, (outcome, router["value"].writes, stored()),
          ([{"node_id": 41, "result": result}], writes, (result, "10.66.66.0/23", router_address, 0)))

router["value"] = FakeRouter([], fail=True)
job.run_once()
job.run_once()
check("an unreachable node is recorded and counted, nothing changed", stored(), ("unreachable", "10.66.66.0/23", None, 2))
router["value"] = FakeRouter(["10.66.66.1/23"])
job.run_once()
check("the next successful run resets the failure count", stored(), ("in_sync", "10.66.66.0/23", "10.66.66.1/23", 0))
check("a disabled node is not visited", [r["node_id"] for r in job.run_once()], [41])

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
