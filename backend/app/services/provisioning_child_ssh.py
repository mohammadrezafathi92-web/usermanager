"""Private SSH/SFTP wrappers for future Xray child actions, no live wiring.

No raw transport/channel escape hatch is exposed. Exact generated read
commands and bounded config-file reads are permitted; every other command,
file open that may write and each subsequent file write needs fresh proof.
"""
import re
from pathlib import PurePosixPath

from . import provisioning_child_authority as authority
from .xray_client import XrayClient


class ChildSshUnavailable(RuntimeError):
    pass


def _safe_path(path):
    return isinstance(path, str) and bool(re.fullmatch(r"/[A-Za-z0-9_./-]+", path)) and (
        ".." not in path.split("/") and "//" not in path and str(PurePosixPath(path)) == path)


class _File:
    def __init__(self, raw, guard, writing):
        self._raw, self._guard, self._writing = raw, guard, writing

    def read(self, *args):
        return self._raw.read(*args)

    def readline(self, *args):
        return self._raw.readline(*args)

    def write(self, data):
        if not self._writing:
            raise ChildSshUnavailable("child_sftp_file_read_only")
        self._guard()
        return self._raw.write(data)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        if self._writing:
            self._guard()
        return self._raw.flush()

    def close(self):
        # All opens are unbuffered: closing cannot flush a buffered write
        # after authority has been revoked. Mutation calls check separately.
        return self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()


class _Sftp:
    def __init__(self, raw, owner):
        self._raw, self._owner = raw, owner

    def open(self, filename, mode="r", bufsize=-1):
        if filename not in self._owner._files or not isinstance(mode, str):
            raise ChildSshUnavailable("child_sftp_path_invalid")
        writing = mode not in ("r", "rb")
        if writing and filename not in self._owner._write_files:
            raise ChildSshUnavailable("child_sftp_write_path_invalid")
        if writing:
            self._owner._write()
        return _File(self._raw.open(filename, mode, bufsize=0), self._owner._write, writing)

    file = open

    def close(self):
        return self._raw.close()


class ChildSshTransport:
    def __init__(self, raw, node_id, config_path, service_name, api_address, *, timeout=15):
        if type(node_id) is not int or node_id < 1 or not _safe_path(config_path) or not isinstance(service_name, str) or (
                not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,127}", service_name)) or not isinstance(api_address, str) or (
                not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9_.-]*|\[[0-9A-Fa-f:]+\]):[0-9]{1,5}", api_address)) or (
                not 1 <= int(api_address.rsplit(":", 1)[1]) <= 65535) or (
                type(timeout) not in (int, float) or not 0 < timeout <= 120):
            raise ChildSshUnavailable("child_ssh_binding_invalid")
        self._raw, self._node_id, self._timeout = raw, node_id, timeout
        candidates = frozenset(XrayClient._CANDIDATE_CONFIG_PATHS) | {config_path}
        self._write_files = frozenset((config_path, config_path + ".tmp", config_path + ".bak"))
        self._files = candidates | self._write_files
        self._read_commands = frozenset(
            [f"test -f {path} && echo __FOUND__" for path in candidates] +
            [f"systemctl show -p ExecStart --no-pager {service} 2>/dev/null; true"
             for service in {service_name, "xray", "v2ray"}] +
            [f"xray api statsquery -s {api_address} -pattern 'user>>>' -json",
             "find / -xdev -maxdepth 6 -iname 'config.json' "
             "\\( -ipath '*xray*' -o -ipath '*v2ray*' \\) 2>/dev/null | head -5"])
        self.write_attempted = False

    def _write(self):
        authority.require_write(self._node_id, "xray_ssh")
        self.write_attempted = True

    def exec_command(self, command, **kwargs):
        if not isinstance(command, str) or command not in self._read_commands:
            self._write()
        kwargs["timeout"] = self._timeout
        return self._raw.exec_command(command, **kwargs)

    def open_sftp(self):
        return _Sftp(self._raw.open_sftp(), self)

    def close(self):
        return self._raw.close()


class ChildXrayClient(XrayClient):
    def __init__(self, *args, node_id, **kwargs):
        super().__init__(*args, **kwargs)
        # Validate before any SSH connection or authentication bytes.
        ChildSshTransport(None, node_id, self.config_path, self.service_name, self.api_address, timeout=self.timeout)
        self._bound_node_id = node_id

    def connect(self):
        super().connect()
        self._client = ChildSshTransport(self._client, self._bound_node_id, self.config_path,
            self.service_name, self.api_address, timeout=self.timeout)
        return self
