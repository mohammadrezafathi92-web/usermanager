"""SSH wrappers use recording objects only; no process, socket or node."""
import io
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _no_network
_no_network.install([])
from app.services import provisioning_child_ssh as ssh
from app.services import provisioning_child_authority as authority


class Raw:
    def __init__(self):
        self.calls = []
        self.files = []
    def exec_command(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return (None, None, None)
    def open_sftp(self):
        return self
    def open(self, path, mode, bufsize):
        self.files.append((path, mode, bufsize))
        return io.BytesIO(b"{}")
    def close(self):
        pass


def refused(callback):
    try:
        callback()
        raise AssertionError("unguarded SSH writer accepted")
    except (authority.WriteAuthorityUnavailable, ssh.ChildSshUnavailable, AttributeError):
        pass


raw = Raw()
guard = ssh.ChildSshTransport(raw, 7, "/etc/xray/config.json", "xray", "127.0.0.1:10085")
read = "systemctl show -p ExecStart --no-pager xray 2>/dev/null; true"
guard.exec_command(read, timeout=None)
assert len(raw.calls) == 1 and raw.calls[-1][1]["timeout"] == 15 and not guard.write_attempted
for command in (read + "; touch /tmp/unsafe", "systemctl restart xray", "xray api statsquery -reset", "FutureCommand"):
    refused(lambda command=command: guard.exec_command(command))
assert len(raw.calls) == 1
refused(lambda: guard.get_transport())
sftp = guard.open_sftp()
refused(lambda: sftp.remove("/etc/xray/config.json"))
refused(lambda: sftp.open("/etc/shadow", "r"))
with sftp.open("/etc/xray/config.json", "rb", bufsize=4096) as reader:
    assert reader.read() == b"{}"
    refused(lambda: reader.write(b"unsafe"))
assert raw.files[-1][2] == 0
for mode in ("w", "a", "r+", "wb", "x"):
    refused(lambda mode=mode: sftp.open("/etc/xray/config.json.tmp", mode))
assert len(raw.files) == 1 and not guard.write_attempted
with patch.object(authority, "require_write") as proof:
    refused(lambda: sftp.open("/etc/v2ray/config.json", "w"))
    guard.exec_command("systemctl restart xray")
    with sftp.open("/etc/xray/config.json.tmp", "wb") as writer:
        writer.write(b"one")
        writer.writelines([b"two", b"three"])
        writer.flush()
        assert proof.call_count == 6 and proof.call_args.args == (7, "xray_ssh")
        with patch.object(authority, "require_write", side_effect=authority.WriteAuthorityUnavailable("lost")):
            refused(lambda: writer.write(b"must-not-be-written"))
    assert guard.write_attempted and raw.files[-1][2] == 0
for path in ("/etc/xray/../shadow", "/etc/xray/config.json;reboot", "/etc//xray/config.json", "relative.json"):
    refused(lambda path=path: ssh.ChildSshTransport(raw, 7, path, "xray", "127.0.0.1:10085"))
for service in ("xray;reboot", "--danger", "xray $(reboot)"):
    refused(lambda service=service: ssh.ChildSshTransport(raw, 7, "/etc/xray/config.json", service, "127.0.0.1:10085"))
assert _no_network.attempts == []
print("PASS private SSH/SFTP: exact reads, unknown writes, direct channel denial, fresh file-write proof, unbuffered close, deadlines")
