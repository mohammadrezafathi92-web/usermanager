"""Installer update dispatches before env/state/source/deployment mutations.

Use a fixture copy with only its root check disabled for unprivileged CI.
Every meaningful updater/installer line remains unchanged. No sudo, daemon,
network, or production path is used.
"""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / "install.sh").read_text()
dispatch = source.index('if [[ "$CLI_ACTION" == "update" ]]; then')
assert dispatch < source.index('mkdir -p "$INSTALL_DIR"')
assert dispatch < source.index('echo "HOST_PROJECT_DIR=$INSTALL_DIR"')
assert dispatch < source.index('echo "$INSTALL_DIR" > "$STATE_DIR/install_dir"')
assert 'exec bash "$INSTALL_DIR/update.sh"' in source
# Both pre-existing management entry points still reach install.sh update.
assert 'bash "$SCRIPT_SRC" update' in source
assert '6) bash "$INSTALL_DIR/install.sh" update' in source

with tempfile.TemporaryDirectory(prefix="installer-safe-update-") as temp:
    folder = Path(temp)
    script = folder / "install-fixture.sh"
    guarded = 'if [[ ${EUID:-$(id -u)} -ne 0 ]]; then'
    assert source.count(guarded) == 1
    script.write_text(source.replace(guarded, "if false; then", 1))
    install = folder / "existing installation"
    (install / "backend/scripts").mkdir(parents=True)
    (install / "backend/scripts/update_preflight.py").write_text("# fixture\n")
    sentinel = install / ".env"
    sentinel.write_text("UNCHANGED=original\n")
    marker = folder / "dispatch"
    (install / "update.sh").write_text('printf "SAFE_DISPATCH" > "$TEST_DISPATCH_MARKER"\nexit "${TEST_UPDATER_EXIT:-0}"\n')
    env = {**os.environ, "TEST_DISPATCH_MARKER": str(marker)}
    run = subprocess.run(["bash", str(script), "update", "--dir", str(install)], env=env, capture_output=True)
    assert run.returncode == 0, run.stderr
    assert marker.read_text() == "SAFE_DISPATCH"
    assert sentinel.read_text() == "UNCHANGED=original\n"
    assert not (install / "docker-compose.yml").exists()
    # Updater failure propagates, rather than falling through to installer.
    run = subprocess.run(["bash", str(script), "update", "--dir", str(install)], env={**env, "TEST_UPDATER_EXIT": "17"}, capture_output=True)
    assert run.returncode == 17
    for option in (["--port", "8080"], ["--repo", "https://example.invalid/repo"], ["--source", "archive.tar.gz"], ["--unknown"]):
        marker.unlink(missing_ok=True)
        run = subprocess.run(["bash", str(script), "update", "--dir", str(install), *option], env=env, capture_output=True)
        assert run.returncode != 0
        assert not marker.exists()
        assert sentinel.read_text() == "UNCHANGED=original\n"
    (install / "backend/scripts/update_preflight.py").unlink()
    run = subprocess.run(["bash", str(script), "update", "--dir", str(install)], env=env, capture_output=True)
    assert run.returncode != 0
    assert not marker.exists()

print("installer safe-update dispatch: PASS (no config/source mutations, errors propagated)")
