"""A persisted strict gate must not become off merely because schema is bad."""
import datetime as dt
import os
import sys
import tempfile
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import models_provisioning as mp
from app.services import node_gate as gate, provisioning_schema as schema


def scenario(engine):
    Factory = sessionmaker(bind=engine)
    assert schema.bootstrap(engine, Factory)["ready"]
    db = Factory()
    row = db.get(mp.ProvisioningRuntimeState, 1)
    identity = dict(lock_backend="flock", lock_verified_at=dt.datetime.utcnow(),
        owner_state="active", owner_host_id="a" * 64, owner_boot_id=str(uuid.uuid4()),
        owner_claimed_at=dt.datetime.utcnow(), owner_heartbeat_at=dt.datetime.utcnow())
    read = gate.read_runtime
    try:
        with patch.object(gate, "read_runtime", side_effect=lambda factory=None: read(Factory)):
            for mode in ("enforced", "shadow", "off"):
                for name, value in identity.items():
                    setattr(row, name, value)
                row.gate_mode = mode
                db.commit()
                schema.mark_not_ready("injected invalid schema, not a mode change")
                gate.reset_cache()  # Actual restart: there is NO previous mode in memory.
                ran = []
                try:
                    with gate.writer_gate(SimpleNamespace(id=1)):
                        ran.append(True)
                except gate.GateNotAvailable:
                    assert mode != "off"
                assert ran == ([True] if mode == "off" else []), (mode, ran)
                assert db.get(mp.ProvisioningRuntimeState, 1).gate_mode == mode
            # A cached stricter mode is retained when even the raw state is unreadable.
            gate._cached_runtime = gate.GateRuntime(str(uuid.uuid4()), "enforced", 1)
            gate._checked_at = None
            with patch.object(gate, "read_runtime", return_value=None):
                try:
                    with gate.writer_gate(SimpleNamespace(id=1)):
                        raise AssertionError("unreadable cached strict gate bypassed")
                except gate.GateNotAvailable:
                    pass
    finally:
        gate.reset_cache()
        db.close()
    print("PASS", engine.dialect.name, "restart with unverified schema never silently relaxes a known strict gate")


with tempfile.TemporaryDirectory(prefix="um-gate-startup-") as directory:
    engine = create_engine("sqlite:///" + os.path.join(directory, "test.db"))
    try:
        scenario(engine)
    finally:
        engine.dispose()
url = os.environ.get("MARIADB_TEST_URL", "").strip()
if url:
    engine = scratch.claim(url)
    try:
        scenario(engine)
    finally:
        scratch.release(engine)
elif os.environ.get("CI", "").lower() == "true":
    raise AssertionError("CI requires real MariaDB")
else:
    print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
