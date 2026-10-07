"""Conservative Linux mountinfo validation without accessing nodes or DNS."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _no_network
_no_network.install()
from app.services import provisioning_lock_verification as verification


def entry(mount, kind, number=1):
    escaped = mount.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011").replace("\n", "\\012")
    return f"{number} 0 0:{number} / {escaped} rw - {kind} source rw\n"


def check(directory, contents, accepted):
    with patch.object(verification.sys, "platform", "linux"), patch.object(Path, "read_text", return_value=contents):
        try:
            verification._local_filesystem(directory)
        except verification.VerificationFailed as exc:
            assert not accepted and str(exc) == "lock_filesystem_unverified", (contents, exc)
        else:
            assert accepted, contents


with tempfile.TemporaryDirectory(prefix="um-mounts-") as directory:
    target = str(Path(directory).resolve())
    root = entry("/", "overlay")
    check(directory, root, True)
    for kind in ("nfs", "nfs4", "cifs", "smb", "smb3", "9p", "fuse.sshfs"):
        check(directory, root + entry(target, kind, 2), False)
    check(directory, root + entry(target, "ext4", 2), True)
    # Both orders and even two apparently local mounts are ambiguous.
    for pair in (("nfs", "zfs"), ("zfs", "nfs"), ("ext4", "zfs")):
        check(directory, root + entry(target, pair[0], 2) + entry(target, pair[1], 3), False)
    check(directory, entry("/", "nfs") + entry(target, "ext4", 2), True)
    check(directory, root + entry(target + "-not-ancestor", "nfs", 2), True)
    for suffix in ("a b", "a\tb", "a\nb", "a\\b"):
        child = Path(directory) / suffix
        child.mkdir()
        check(str(child), root + entry(str(child.resolve()), "nfs", 2), False)
        check(str(child), root + entry(str(child.resolve()), "ext4", 2), True)
    for malformed in ("", "broken", "1 0 0:1 / /\\bad rw - ext4 source rw\n"):
        check(directory, malformed, False)
    with patch.object(verification.sys, "platform", "darwin"):
        try:
            verification._local_filesystem(directory)
            raise AssertionError("unsupported host accepted")
        except verification.VerificationFailed as exc:
            assert str(exc) == "lock_verification_requires_linux"
assert _no_network.attempts == []
print("PASS mountinfo: exact boundary, longest mount, ambiguous stacks, network filesystems, all escaped path characters")
