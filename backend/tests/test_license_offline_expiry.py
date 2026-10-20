"""Scheduled local expiry progresses even when the control server is offline.

Only throwaway signing keys, an isolated state file and a frozen clock are
used. No network request, production key or real state file is accessed.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "isolated-license-expiry-test-key-not-production")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import settings
from app.services import license_state, licensing

START = dt.datetime(2026, 10, 20, 12)


class Clock(dt.datetime):
    now_value = START

    @classmethod
    def utcnow(cls):
        return cls.now_value


private = Ed25519PrivateKey.generate()
b64 = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")
public = b64(private.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw,
))
checks = 0


def check(label, actual, expected):
    global checks
    assert actual == expected, f"{label}: got {actual!r}, expected {expected!r}"
    checks += 1
    print("PASS", label)


def issue(*, days=1, valid_days=None):
    payload = licensing.LicensePayload(
        license_id="isolated-offline-expiry", customer="test", fingerprint="",
        issued_at=START, expires_at=(START + dt.timedelta(days=days)) if days else None,
        valid_days=valid_days,
    )
    body = json.dumps(payload.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    return f"{licensing.TOKEN_PREFIX}.{b64(body)}.{b64(private.sign(body))}"


def reset(path, *, days=1, valid_days=None, silent_days=0, state=None):
    Clock.now_value = START
    path.write_text(json.dumps(state or {}))
    license_state._cached = None
    settings.license_master_install = False
    settings.license_server_url = "https://isolated.invalid"
    settings.license_lock_after_silent_days = silent_days
    settings.license_key = issue(days=days, valid_days=valid_days)
    return license_state.refresh()


with tempfile.TemporaryDirectory(prefix="license_expiry_") as folder:
    state_path = Path(folder) / "state.json"
    with (
        patch.object(license_state, "_STATE_PATH", str(state_path)),
        patch.object(license_state, "dt", SimpleNamespace(datetime=Clock, timedelta=dt.timedelta)),
        patch.object(licensing, "SIGNING_PUBLIC_KEY_B64", public),
        patch.object(licensing, "hardware_fingerprint", return_value="isolated"),
        patch.object(license_state, "get_build_info", return_value={"version": "test"}),
        patch.object(license_state.urllib.request, "urlopen", side_effect=OSError("offline")) as network,
    ):
        check("fixed licence initially valid", reset(state_path).lock_panel, False)
        Clock.now_value = START + dt.timedelta(days=2)
        license_state.heartbeat_job()
        check("fixed expiry advances despite timeout", license_state.current_enforcement().reason,
              licensing.REASON_EXPIRED)
        check("timeout did not invent an online check", "last_online_check" in license_state._load_state(), False)

        reset(state_path, days=None, valid_days=1)
        activation = license_state._load_state()["activations"]
        Clock.now_value = START + dt.timedelta(days=2)
        license_state.heartbeat_job()
        check("activation-dated expiry advances offline", license_state.current_enforcement().reason,
              licensing.REASON_EXPIRED)
        check("activation is not restarted", license_state._load_state()["activations"], activation)

        reset(state_path, days=365, state={"last_online_check": START.isoformat()})
        Clock.now_value = START + dt.timedelta(days=30)
        license_state.heartbeat_job()
        check("default silence policy still permits a paid licence", license_state.current_enforcement().lock_panel, False)

        reset(state_path, days=365, silent_days=7, state={"last_online_check": START.isoformat()})
        Clock.now_value = START + dt.timedelta(days=8)
        license_state.heartbeat_job()
        check("explicit silence window still applies", license_state.current_enforcement().reason,
              licensing.REASON_GRACE_EXHAUSTED)

        reset(state_path, state={"revoked": True, "lock_scope": "panel_and_bot"})
        license_state.heartbeat_job()
        check("offline refresh retains revocation", license_state.current_enforcement().reason,
              licensing.REASON_REVOKED)
        check("offline refresh retains selected scope", license_state.current_enforcement().lock_bot, True)

        reset(state_path)
        settings.license_server_url = ""
        previous_requests = network.call_count
        Clock.now_value = START + dt.timedelta(days=2)
        license_state.heartbeat_job()
        check("no-server path still expires", license_state.current_enforcement().reason,
              licensing.REASON_EXPIRED)
        check("no-server path makes no request", network.call_count, previous_requests)

        settings.license_master_install = True
        license_state.heartbeat_job()
        check("master install remains exempt", license_state.current_enforcement().lock_panel, False)

print(f"{checks} checks passed")
