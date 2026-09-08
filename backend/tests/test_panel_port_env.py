"""services/local_deploy.py's panel-port-change feature, and self_update.py's
handling of it across a `git pull`.

Run:  python3 backend/tests/test_panel_port_env.py

Bug reported 2026-09-08: after changing "پورت پنل وب" from Settings, a
later update (pulling code that also touched docker-compose.yml) made the
port silently revert to 80. Root cause: change_panel_port_local() used to
rewrite the literal "CURRENT:80" string directly inside docker-compose.yml
- a file tracked by git - so the port change showed up as a local
modification that any future `git pull` touching the same file either
refused outright or, once force-resolved, discarded. Fixed by moving the
port into PANEL_WEB_PORT in the repo-root .env (gitignored, the same
established home as HOST_PROJECT_DIR/COMPOSE_PROFILES/MARIADB_*) with
docker-compose.yml's frontend service now mapping
"${PANEL_WEB_PORT:-80}:80" instead of a literal port.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


tmp_project_dir = tempfile.mkdtemp()
os.environ["HOST_PROJECT_DIR"] = tmp_project_dir

from app.services import local_deploy  # noqa: E402
from app.services import self_update  # noqa: E402

# A minimal docker-compose.yml so change_panel_port_local's own
# "does the file exist" check passes - its content is never parsed for the
# port anymore, only .env is.
compose_path = os.path.join(tmp_project_dir, "docker-compose.yml")
with open(compose_path, "w", encoding="utf-8") as fh:
    fh.write('services:\n  frontend:\n    ports:\n      - "${PANEL_WEB_PORT:-80}:80"\n')

env_path = os.path.join(tmp_project_dir, ".env")

print("--- _root_env_path resolves under HOST_PROJECT_DIR ---")
check("repo-root .env, not backend/.env", local_deploy._root_env_path(), env_path)

print("\n--- _read_env_var / _write_env_var round trip ---")
check("missing file -> None", local_deploy._read_env_var(env_path, "PANEL_WEB_PORT"), None)
with open(env_path, "w", encoding="utf-8") as fh:
    fh.write("COMPOSE_PROFILES=mariadb\n")
local_deploy._write_env_var(env_path, "PANEL_WEB_PORT", "8081")
check("new var appended", local_deploy._read_env_var(env_path, "PANEL_WEB_PORT"), "8081")
with open(env_path, encoding="utf-8") as fh:
    kept = fh.read()
check("unrelated existing var survives", "COMPOSE_PROFILES=mariadb" in kept, True)
local_deploy._write_env_var(env_path, "PANEL_WEB_PORT", "9090")
check("re-writing replaces, not duplicates", local_deploy._read_env_var(env_path, "PANEL_WEB_PORT"), "9090")
with open(env_path, encoding="utf-8") as fh:
    check("...exactly one PANEL_WEB_PORT line", fh.read().count("PANEL_WEB_PORT="), 1)

original_verify = local_deploy.verify_host_path
original_ensure = local_deploy.ensure_docker_compose_cli
original_run = local_deploy._run
local_deploy.verify_host_path = lambda: tmp_project_dir  # no real docker daemon in this test

print("\n--- change_panel_port_local: safety check against actual .env state ---")
local_deploy._write_env_var(env_path, "PANEL_WEB_PORT", "9090")
try:
    local_deploy.change_panel_port_local(current_port=80, new_port=8080)
    check("mismatched current_port raises", False, True)
except local_deploy.DeployError:
    check("mismatched current_port raises", True, True)
check("...and .env was NOT touched", local_deploy._read_env_var(env_path, "PANEL_WEB_PORT"), "9090")

print("\n--- change_panel_port_local: happy path writes .env, never touches docker-compose.yml ---")
local_deploy._write_env_var(env_path, "PANEL_WEB_PORT", "80")  # simulates an install still on the default port
local_deploy.ensure_docker_compose_cli = lambda: "/fake/docker-compose"
local_deploy._run = lambda cmd, timeout=60: (0, "", "")
try:
    with open(compose_path, encoding="utf-8") as fh:
        compose_before = fh.read()
    log = local_deploy.change_panel_port_local(current_port=80, new_port=8080)
    check("returns a log string", isinstance(log, str) and "8080" in log, True)
    check("PANEL_WEB_PORT written to .env", local_deploy._read_env_var(env_path, "PANEL_WEB_PORT"), "8080")
    with open(compose_path, encoding="utf-8") as fh:
        check("docker-compose.yml itself is untouched", fh.read(), compose_before)

    print("\n--- change_panel_port_local: a failed `up -d` rolls the .env value back ---")
    local_deploy._run = lambda cmd, timeout=60: (1, "", "boom")
    try:
        local_deploy.change_panel_port_local(current_port=8080, new_port=9000)
        check("failure raises DeployError", False, True)
    except local_deploy.DeployError:
        check("failure raises DeployError", True, True)
    check("...and .env was rolled back to the pre-attempt port", local_deploy._read_env_var(env_path, "PANEL_WEB_PORT"), "8080")
finally:
    local_deploy.verify_host_path = original_verify
    local_deploy.ensure_docker_compose_cli = original_ensure
    local_deploy._run = original_run

print("\n--- self_update._restore_frontend_port: old literal-port installs still get rewritten in place ---")
with open(compose_path, "w", encoding="utf-8") as fh:
    fh.write('services:\n  frontend:\n    ports:\n      - "8081:80"\n')
self_update._restore_frontend_port("9091")
with open(compose_path, encoding="utf-8") as fh:
    check("old-format compose file gets the literal port rewritten", '"9091:80"' in fh.read(), True)

print("\n--- self_update._restore_frontend_port: new-format installs fall back to .env (the migration path) ---")
with open(compose_path, "w", encoding="utf-8") as fh:
    fh.write('services:\n  frontend:\n    ports:\n      - "${PANEL_WEB_PORT:-80}:80"\n')
if os.path.exists(env_path):
    os.remove(env_path)
self_update._restore_frontend_port("8082")
check("no literal port to rewrite -> written to .env instead", local_deploy._read_env_var(env_path, "PANEL_WEB_PORT"), "8082")
with open(compose_path, encoding="utf-8") as fh:
    check("...and docker-compose.yml itself stays untouched", "${PANEL_WEB_PORT:-80}" in fh.read(), True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
