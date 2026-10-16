"""The operator's host secret survives updates and is never silently repaired."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "backend/scripts/ensure_host_identity.sh"


def run(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), str(path)], capture_output=True, text=True)


compose = (ROOT / "docker-compose.yml").read_text()
assert "- /etc/usermanager:/etc/usermanager:ro" in compose
assert "bash backend/scripts/ensure_host_identity.sh" in (ROOT / "install.sh").read_text()
assert "bash backend/scripts/ensure_host_identity.sh" in (ROOT / "update.sh").read_text()

if sys.platform == "linux":
    from app.services.provisioning_host import read_identity

    with tempfile.TemporaryDirectory(prefix="host-identity-test-") as folder:
        directory = Path(folder)
        path = directory / "state" / "host.id"
        first = run(path)
        assert first.returncode == 0, first.stderr
        original = path.read_bytes()
        assert len(original) == 32
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

        boot = directory / "boot.id"
        boot.write_text(str(uuid.uuid4()))
        host_id = read_identity(secret_path=str(path), boot_path=str(boot)).host_id

        again = run(path)
        assert again.returncode == 0, again.stderr
        assert path.read_bytes() == original
        assert read_identity(secret_path=str(path), boot_path=str(boot)).host_id == host_id

        path.write_bytes(b"short")
        assert run(path).returncode != 0
        assert path.read_bytes() == b"short"  # malformed identity is not rotated

        path.unlink()
        victim = directory / "victim"
        victim.write_bytes(b"keep")
        path.symlink_to(victim)
        assert run(path).returncode != 0
        assert victim.read_bytes() == b"keep"

        path.unlink()
        path.write_bytes(original)
        os.chmod(path, 0o644)
        assert run(path).returncode != 0
        assert path.read_bytes() == original

        assert run(Path("relative/host.id")).returncode != 0

print("host identity bootstrap: OK")
