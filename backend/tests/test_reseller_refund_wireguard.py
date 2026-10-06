"""No network: verified stop, durable measurement, then exact deletion."""
import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.adapter_mikrotik_wg import WireguardIdentity, StoppedUsage, stop_capture_remove
from app.services.adapter_base import AdapterError


IDENT = WireguardIdentity("wg0", "owned-peer", "owned-public-key", "10.0.0.2/32")


class Router:
    def __init__(self):
        self.peers = [{".id": "*1", "interface": "wg0", "comment": IDENT.peer_name,
                       "public-key": IDENT.public_key, "allowed-address": IDENT.client_address,
                       "disabled": False, "tx": "30", "rx": "10"}]
        self.log = []
        self.unreadable = False
        self.disable_lies = False
        self.delete_lies = False
        self.queue_fails = False
        self.counters_move = False

    def list_peers(self, interface):
        self.log.append("read")
        if self.unreadable:
            raise RuntimeError("offline")
        if self.counters_move and self.peers[0]["disabled"]:
            self.peers[0]["tx"] = str(int(self.peers[0]["tx"]) + 1)
        return [dict(p) for p in self.peers]

    def set_peer_disabled(self, handle, disabled):
        assert handle == "*1" and disabled is True
        self.log.append("disable")
        if not self.disable_lies:
            self.peers[0]["disabled"] = True

    def remove_peer(self, handle):
        assert handle == "*1"
        self.log.append("delete")
        if not self.delete_lies:
            self.peers.clear()

    def remove_simple_queue(self, name):
        assert name == "um-speed-owned-peer"
        self.log.append("queue")
        if self.queue_fails:
            raise RuntimeError("offline")


def fails(router, code, persist=lambda usage: usage, prior=None):
    try:
        stop_capture_remove(router, IDENT, persist_usage=persist, previously_captured=prior)
    except AdapterError as exc:
        assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError("unsafe operation accepted")


def main():
    router = Router()
    durable = []
    def persist(usage):
        router.log.append("commit-usage")
        durable.append(usage)
        return usage
    result = stop_capture_remove(router, IDENT, persist_usage=persist)
    assert result == StoppedUsage(30, 10)
    assert router.log.index("disable") < router.log.index("commit-usage") < router.log.index("delete")
    assert router.peers == [] and len(durable) == 1
    assert stop_capture_remove(router, IDENT, persist_usage=persist, previously_captured=durable[0]) == result
    assert len(durable) == 1 and router.log.count("delete") == 1

    router = Router()
    router.peers[0]["public-key"] = "someone-else"
    fails(router, "conflict_name_taken")
    assert "disable" not in router.log and "delete" not in router.log
    router = Router()
    router.unreadable = True
    fails(router, "wg_refund_unreadable")
    assert "delete" not in router.log
    router = Router()
    router.peers.clear()
    fails(router, "wg_refund_usage_missing")
    for bad in [None, "", "-1", "1.5", True]:
        router = Router()
        router.peers[0]["tx"] = bad
        fails(router, "wg_refund_counters_unreadable")
        assert "delete" not in router.log
    router = Router()
    router.disable_lies = True
    fails(router, "wg_refund_stop_unconfirmed")
    assert "delete" not in router.log
    router = Router()
    router.counters_move = True
    fails(router, "wg_refund_counters_not_stable")
    assert "delete" not in router.log
    router = Router()
    fails(router, "wg_refund_usage_not_persisted", persist=lambda _: None)
    assert "delete" not in router.log
    router = Router()
    router.delete_lies = True
    fails(router, "wg_refund_delete_unconfirmed")
    assert router.peers, "success must not be inferred from a delete answer"

    # Crash/queue failure after peer deletion: durable counters survive;
    # retry completes cleanup without assuming the now-absent peer was free.
    router = Router()
    saved = []
    def save(usage):
        saved.append(usage)
        return usage
    router.queue_fails = True
    fails(router, "wg_refund_delete_unconfirmed", persist=save)
    assert router.peers == [] and saved == [StoppedUsage(30, 10)]
    router.queue_fails = False
    assert stop_capture_remove(router, IDENT, persist_usage=save, previously_captured=saved[0]) == saved[0]
    assert len(saved) == 1

    router = Router()
    def crash(_):
        raise RuntimeError("DB commit failed")
    try:
        stop_capture_remove(router, IDENT, persist_usage=crash)
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed durable write ignored")
    assert "delete" not in router.log
    print("PASS: WireGuard stop/capture/delete/retry and adversarial errors (no network)")


if __name__ == "__main__":
    main()
