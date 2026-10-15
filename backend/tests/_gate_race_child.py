"""Child process of tests/test_gate_transition_race.py (not a test itself).

    python tests/_gate_race_child.py write <node_id>   one legacy RouterOS write WITHOUT any
                                                        node_gate passage; prints sent|refused
    python tests/_gate_race_child.py miss <node_id>    one 'off' passage whose gate-mode lock
                                                        file is unusable; prints missed

DATABASE_URL and UM_LOCK_DIR come from the parent. No network but the
scratch database; the RouterOS API is a fake that records what it was sent.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([os.environ.get("DATABASE_URL", "")])

from app import models
from app.database import SessionLocal, engine
from app.services import node_gate, provisioning_schema, transport_guard
from app.services.mikrotik_client import MikrotikClient


class FakeRouteros:
    def __init__(self):
        self.sent = []

    def __call__(self, command, **kwargs):
        if not command.endswith("/print"):
            self.sent.append(command)
        yield {"ret": "*1"}

    def close(self):
        pass


def main():
    action, node_id = sys.argv[1], int(sys.argv[2])
    provisioning_schema.bootstrap(engine, SessionLocal)
    with SessionLocal() as db:
        node = db.get(models.Node, node_id)
        db.expunge(node)
    if action == "miss":
        blocker = os.path.join(os.environ["UM_LOCK_DIR"], "not-a-directory")
        open(blocker, "a").close()
        os.environ["UM_LOCK_DIR"] = os.path.join(blocker, "locks")       # unusable: a file in the path
        with node_gate.writer_gate(node):
            pass
        print("missed", flush=True)
        return
    client = MikrotikClient.for_node(node)
    raw = FakeRouteros()
    client._api = transport_guard.RouterosApi(raw, client)
    try:
        client._api.path("ip", "address").add(address="10.99.0.1/32")
        print("sent" if raw.sent else "nothing", flush=True)
    except transport_guard.GateViolation:
        print("refused" if not raw.sent else "refused-after-send", flush=True)


if __name__ == "__main__":
    main()
