"""update.sh on a fresh install (88.218.18.14, 2026-11-02):

1. backup must not fail when the bot database was never created;
2. the after-restart check must accept the ONE ownership change a clean
   restart makes outside 'enforced' (shutdown_release: active -> released,
   ownership_epoch + 1) and still refuse every other runtime change.

Run:  python3 backend/tests/test_update_fresh_install.py
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SECRET_KEY", "update-fresh-install-test-only-0123456789")
sys.path.insert(0, str(ROOT / "backend"))
spec = importlib.util.spec_from_file_location("update_preflight", ROOT / "backend/scripts/update_preflight.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

from app.config import settings  # noqa: E402

failures: list[str] = []


def check(label, got, expected):
    if callable(got):
        try:
            got = got()
        except Exception as exc:  # noqa: BLE001 - one FAIL, not an abort
            got = f"raised {type(exc).__name__}: {exc}"
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def refused(function):
    try:
        function()
    except Exception:  # noqa: BLE001
        return True
    return False


with tempfile.TemporaryDirectory(prefix="update-fresh-") as temp:
    folder = Path(temp)
    panel = folder / "panel.db"
    with sqlite3.connect(panel) as db:
        db.execute("CREATE TABLE proof (value TEXT)")
    with patch.object(settings, "database_url", "sqlite:///" + str(panel)), patch.dict(os.environ, {
        "BACKUP_DIR": str(folder / "backups"), "BOT_DB_PATH": str(folder / "never-created" / "bot_data.db"),
    }):
        manifest = None
        try:
            manifest = helper.create_backup("update_fresh")
        except Exception as exc:  # noqa: BLE001
            print(f"        create_backup raised {type(exc).__name__}: {exc}")
        check("backup succeeds without a bot database", manifest is not None, True)
        if manifest:
            backup = Path(manifest["directory"])
            check("no bot.db member is recorded", "bot.db" in manifest["files"], False)
            check("the bot database is reported absent", manifest.get("bot_database"), "absent")
            check("panel database is still backed up", "panel.db" in manifest["files"], True)
            check("backup verifies", lambda: helper.verify_backup(backup) or "ok", "ok")
            check("nothing is created at the bot path", (folder / "never-created").exists(), False)
    with patch.object(settings, "database_url", "sqlite:///" + str(panel)), patch.dict(os.environ, {
        "BACKUP_DIR": str(folder / "backups"), "BOT_DB_PATH": str(folder / "bot_data.db"),
    }):
        (folder / "bot_data.db").write_bytes(b"not a database")
        check("an existing but broken bot database still fails the backup",
              refused(lambda: helper.create_backup("update_broken_bot")), True)

    base = {
        "provisioning_runtime_state": {"gate_mode": "shadow", "gate_mode_epoch": 1, "owner_state": "active",
                                       "ownership_epoch": 3, "lock_backend": "flock+get_lock"},
        "wallet_runtime_state": {"phase": "normal", "epoch": 0, "external_fencing": "none"},
        "provisioning_type_modes": [["create_user", "legacy"]],
    }

    def released(state, epoch_step=1):
        state = copy.deepcopy(state)
        row = state["provisioning_runtime_state"]
        row["owner_state"], row["ownership_epoch"] = "released", row["ownership_epoch"] + epoch_step
        return state

    def after(before, now):
        a, b = folder / "a.json", folder / "b.json"
        a.write_text(json.dumps(before)); b.write_text(json.dumps(now))
        return helper.compare_after_restart(a, b)

    check("unchanged state passes", lambda: after(base, base), "unchanged")
    check("shutdown release (active -> released, epoch + 1) passes", lambda: after(base, released(base)),
          "ownership_released")
    check("epoch jump of 2 is refused", refused(lambda: after(base, released(base, 2))), True)
    gate = released(base); gate["provisioning_runtime_state"]["gate_mode"] = "off"
    check("gate change together with the release is refused", refused(lambda: after(base, gate)), True)
    modes = copy.deepcopy(base); modes["provisioning_type_modes"] = [["create_user", "durable"]]
    check("type-mode change is refused", refused(lambda: after(base, modes)), True)
    wallet = released(base); wallet["wallet_runtime_state"]["phase"] = "enforced"
    check("wallet change with the release is refused", refused(lambda: after(base, wallet)), True)
    enforced = copy.deepcopy(base); enforced["provisioning_runtime_state"]["gate_mode"] = "enforced"
    check("a release under 'enforced' is refused (it must be kept there)",
          refused(lambda: after(enforced, released(enforced))), True)
    check("released -> active is refused", refused(lambda: after(released(base), base)), True)
    check("the strict compare still refuses the release",
          refused(lambda: helper.compare_strict(base, released(base))), True)

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall fresh-install update checks passed")
