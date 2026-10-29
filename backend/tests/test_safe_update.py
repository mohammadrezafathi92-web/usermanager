"""Safe updater: real local SQLite backups, fake deployment commands only."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
os.environ["SECRET_KEY"] = "safe-update-test-only-not-a-production-secret"
sys.path.insert(0, str(ROOT / "backend"))
spec = importlib.util.spec_from_file_location("update_preflight", ROOT / "backend/scripts/update_preflight.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

from sqlalchemy import create_engine, text
from app.config import settings


def expect_failure(function):
    try:
        function()
    except Exception:
        return
    raise AssertionError("expected refusal")


with tempfile.TemporaryDirectory(prefix="safe-update-") as temp:
    folder = Path(temp)
    panel = folder / "custom panel.db"
    bot = folder / "custom bot.db"
    for path in (panel, bot):
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE proof (value TEXT)")
            db.execute("INSERT INTO proof VALUES ('kept')")
    engine = create_engine("sqlite:///" + str(panel))
    with engine.begin() as db:
        db.execute(text("CREATE TABLE wallet_runtime_state(id INTEGER PRIMARY KEY, phase TEXT, epoch INTEGER, external_fencing TEXT)"))
        db.execute(text("INSERT INTO wallet_runtime_state VALUES (1,'enforced',2,'none')"))
        db.execute(text("CREATE TABLE provisioning_operations(state TEXT)"))
        db.execute(text("INSERT INTO provisioning_operations VALUES ('completed')"))
        db.execute(text("CREATE TABLE wallet_operations(state TEXT)"))
        db.execute(text("INSERT INTO wallet_operations VALUES ('completed')"))
    state = helper.runtime_snapshot(engine)
    assert state["wallet_runtime_state"]["phase"] == "enforced"
    assert state["provisioning_runtime_state"] is None
    for table in ("wallet_operations", "provisioning_operations"):
        with engine.begin() as db:
            db.execute(text(f"INSERT INTO {table} VALUES ('cleanup_required')"))
        expect_failure(lambda: helper.runtime_snapshot(engine))
        with engine.begin() as db:
            db.execute(text(f"DELETE FROM {table} WHERE state='cleanup_required'"))
    with patch.object(settings, "database_url", "sqlite:///" + str(panel)), patch.dict(os.environ, {
        "BACKUP_DIR": str(folder / "custom backups"), "BOT_DB_PATH": str(bot),
    }):
        manifest = helper.create_backup("update_test")
        backup = Path(manifest["directory"])
        assert (backup / "panel.db").stat().st_mode & 0o777 == 0o600
        assert backup.stat().st_mode & 0o777 == 0o700
        helper.verify_backup(backup)
        for name in ("panel.db", "bot.db"):
            with sqlite3.connect(backup / name) as db:
                assert db.execute("SELECT value FROM proof").fetchone() == ("kept",)
        assert "password" not in json.dumps(manifest)
        (backup / "bot.db").write_bytes(b"broken")
        expect_failure(lambda: helper.verify_backup(backup))
        expect_failure(lambda: helper.create_backup("../escape"))
        expect_failure(lambda: helper.sqlite_backup(folder / "missing.db", folder / "not_created.db"))
        assert not (folder / "missing.db").exists()
        expect_failure(lambda: helper.configured_engine("sqlite:///" + str(folder / "missing.db")))
        assert not (folder / "missing.db").exists()
    engine.dispose()

    # MySQL uses only the established dump primitive, never restore/delivery.
    class FakeConnection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): return types.SimpleNamespace(scalar_one=lambda: 0)
    mysql_engine = types.SimpleNamespace(connect=FakeConnection, dispose=lambda: None)
    dump_module = types.ModuleType("app.services.backup")
    dump_module._mysql_dump_bytes = lambda: b"-- consistent test dump\nCREATE TABLE x(id INT);\n"
    with patch.object(settings, "database_url", "mysql+pymysql://user:secret@localhost/db"), patch.dict(os.environ, {
        "BACKUP_DIR": str(folder / "custom backups"), "BOT_DB_PATH": str(bot),
    }), patch.object(helper, "configured_engine", return_value=mysql_engine), patch.object(helper, "runtime_snapshot", return_value=state), patch.dict(sys.modules, {"app.services.backup": dump_module}):
        mysql_manifest = helper.create_backup("update_mysql")
        mysql_folder = Path(mysql_manifest["directory"])
        helper.verify_backup(mysql_folder)
        assert mysql_manifest["panel_validation"] == "dump_exit_status_and_sha256_not_restore_test"
        assert (mysql_folder / "panel.sql").read_bytes() == dump_module._mysql_dump_bytes()
        assert "secret" not in json.dumps(mysql_manifest)
        dump_module._mysql_dump_bytes = lambda: b""
        expect_failure(lambda: helper.create_backup("update_empty_mysql"))

    # Real bash and git in a local clone. Docker/flock/helper are simulated;
    # no daemon, network, node, or production credentials are involved.
    repo = folder / "repo"
    repo.mkdir()
    (repo / "backend/scripts").mkdir(parents=True)
    shutil.copy(ROOT / "update.sh", repo / "update.sh")
    (repo / "backend/scripts/ensure_host_identity.sh").write_text("#!/bin/bash\nexit 0\n")
    (repo / "frontend").mkdir()
    (repo / "backend/Dockerfile").write_text("FROM python:3.11-slim AS base\nFROM base AS runtime\n")
    (repo / "frontend/Dockerfile").write_text("FROM node:20-alpine AS build\nFROM --platform=linux/amd64 nginx:alpine\n")
    fake_helper = '''import json,sys,os
from pathlib import Path
cmd=sys.argv[1]
if cmd=='backup':
 p=Path(sys.argv[2]); p.mkdir(); (p/'runtime.json').write_text('{}')
 manifest={'directory':str(p.resolve()),'files':{}}
 if os.environ.get('UM_UPDATE_STALLED_XRAY_DELETE_ID'):
  op=os.environ['UM_UPDATE_STALLED_XRAY_DELETE_ID']; manifest['operation_hold']={'operation_id':int(op),'lease_owner':f'op:{op}:987654321','fencing_epoch':4}
 print(json.dumps(manifest))
elif cmd=='snapshot': print('{"changed":true}' if os.environ.get('TEST_RUNTIME_DRIFT')=='yes' and Path(os.environ['TEST_RESTART_MARKER']).exists() else '{}')
elif cmd=='compare':
 if json.loads(Path(sys.argv[2]).read_text())!=json.loads(Path(sys.argv[3]).read_text()): sys.exit(1)
elif cmd=='release-operation-hold': print('OPERATION_HOLD_RELEASED')
elif cmd=='validate-operation-hold': print('OPERATION_HOLD_VALID')
'''
    (repo / "backend/scripts/update_preflight.py").write_text(fake_helper)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    def git(*args):
        return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args], cwd=repo, text=True).strip()
    git("add", ".")
    git("commit", "-m", "initial")
    git("remote", "add", "origin", str(repo))
    bin_dir = folder / "bin"
    bin_dir.mkdir()
    (bin_dir / "flock").write_text("#!/bin/bash\nexit 0\n")
    (bin_dir / "timeout").write_text("#!/bin/bash\nshift\nexec \"$@\"\n")
    (bin_dir / "sleep").write_text("#!/bin/bash\nexit 0\n")
    docker = bin_dir / "docker"
    docker.write_text('''#!/bin/bash
echo "$*" >> "$TEST_DOCKER_LOG"
if [[ $2 == up && ${*: -1} == backend ]]; then touch "$TEST_RESTART_MARKER"; fi
if [[ ${TEST_FAIL_FRONTEND:-} == yes && $2 == exec && $4 == frontend ]]; then exit 1; fi
if [[ $1 == compose && $2 == exec && $* == *"python -c"* ]]; then
  [[ ${TEST_FAIL_HEALTH:-} == yes && -f "$TEST_RESTART_MARKER" ]] && exit 1
  exit 0
fi
if [[ $1 == inspect ]]; then echo sha256:old; exit; fi
if [[ $1 == image && $2 == inspect ]]; then [[ ${TEST_IMAGE_MISSING:-} == yes ]] && exit 1; exit 0; fi
if [[ $1 == pull ]]; then
 n=$(grep -c '^pull ' "$TEST_DOCKER_LOG")
 [[ ${TEST_PULL_FAIL:-} == always ]] && exit 1
 [[ ${TEST_PULL_FAIL:-} == once && $n -le 1 ]] && exit 1
 exit 0
fi
if [[ $1 == image ]]; then exit; fi
if [[ $2 == ps ]]; then echo fake-container; exit; fi
if [[ $2 == cp ]]; then src=${3#backend:}; cp -R "$src" "$4"; exit; fi
if [[ $2 == build && ${TEST_FAIL_BUILD:-} == yes ]]; then exit 1; fi
if [[ $1 == compose && $2 == exec ]]; then
 shift 2
 while [[ $# -gt 0 ]]; do
  case "$1" in
   -e) export "$2"; shift 2 ;;
   -T) shift ;;
   backend) shift ;;
   python) shift; exec python3 "$@" ;;
   *) shift ;;
  esac
 done
fi
exit 0
''')
    for file in bin_dir.iterdir():
        file.chmod(0o700)
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
           "TEST_DOCKER_LOG": str(folder / "docker.log"), "TEST_RESTART_MARKER": str(folder / "restarted"),
           "UM_UPDATE_BACKUP_DIR": str(folder / "hostbackups")}
    run = subprocess.run(["bash", "update.sh"], cwd=repo, env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    log = Path(env["TEST_DOCKER_LOG"]).read_text()
    assert "build backend frontend" in log
    assert log.index("build backend frontend") < log.index("up -d --no-build --no-deps backend")
    assert "up -d --no-build --no-deps frontend" in log
    assert "license-server" not in log
    assert "UPDATE_OK" in run.stdout
    check_images = [line for line in log.splitlines() if line.startswith("image inspect")]
    assert sorted(check_images) == ["image inspect nginx:alpine", "image inspect node:20-alpine",
                                    "image inspect python:3.11-slim"], check_images
    assert "pull " not in log, "images already on the host need no network"
    # Explicit operation hold keeps the one unknown Xray delete fenced
    # through both runtime snapshots, then releases it after backend health.
    Path(env["TEST_DOCKER_LOG"]).write_text("")
    held = subprocess.run(["bash", "update.sh", "--target", git("rev-parse", "HEAD"),
                           "--allow-stalled-xray-delete", "182"], cwd=repo, env=env,
                          capture_output=True, text=True)
    assert held.returncode == 0, held.stdout + held.stderr
    hold_log = Path(env["TEST_DOCKER_LOG"]).read_text()
    assert "UM_UPDATE_STALLED_XRAY_DELETE_ID=182" in hold_log
    assert "validate-operation-hold" in hold_log
    assert "release-operation-hold" not in hold_log
    assert "OPERATION_HOLD_PENDING_ADMIN=182" in held.stdout
    assert "UPDATE_OK" in held.stdout
    Path(env["TEST_DOCKER_LOG"]).write_text("")
    held_failure = subprocess.run(["bash", "update.sh", "--target", git("rev-parse", "HEAD"),
                                   "--allow-stalled-xray-delete", "182"], cwd=repo,
                                  env={**env, "TEST_FAIL_BUILD": "yes"}, capture_output=True, text=True)
    assert held_failure.returncode != 0
    failed_hold_log = Path(env["TEST_DOCKER_LOG"]).read_text()
    assert "release-operation-hold" in failed_hold_log
    assert "up -d --no-build --no-deps backend" not in failed_hold_log
    # A base image that cannot be pulled stops BEFORE backup and checkout.
    head = git("rev-parse", "HEAD")
    Path(env["TEST_DOCKER_LOG"]).write_text("")
    backups_before = sorted((folder / "hostbackups").iterdir())
    bad = subprocess.run(["bash", "update.sh"], cwd=repo, env={**env, "TEST_IMAGE_MISSING": "yes",
                         "TEST_PULL_FAIL": "always"}, capture_output=True, text=True)
    assert bad.returncode != 0
    log = Path(env["TEST_DOCKER_LOG"]).read_text()
    assert log.count("pull --quiet nginx:alpine") == 3, log
    assert "build" not in log and "up -d" not in log and "exec -T backend python - backup" not in log
    assert git("rev-parse", "HEAD") == head
    assert sorted((folder / "hostbackups").iterdir()) == backups_before
    assert "could not be pulled" in bad.stderr and "nothing was changed" in bad.stderr
    assert "UPDATE_OK" not in bad.stdout
    # A transient failure is retried (bounded) and the update then proceeds.
    Path(env["TEST_DOCKER_LOG"]).write_text("")
    ok = subprocess.run(["bash", "update.sh"], cwd=repo, env={**env, "TEST_IMAGE_MISSING": "yes",
                        "TEST_PULL_FAIL": "once"}, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "BASE_IMAGE=nginx:alpine pulled" in ok.stdout and "attempt 1 of 3 failed" in ok.stderr
    # Exact-target disagreement must stop before even making a backup.
    Path(env["TEST_DOCKER_LOG"]).write_text("")
    bad = subprocess.run(["bash", "update.sh", "--target", "f" * 40], cwd=repo, env=env, capture_output=True)
    assert bad.returncode != 0
    assert Path(env["TEST_DOCKER_LOG"]).read_text() == ""
    # Build failure cannot restart any service.
    Path(env["TEST_DOCKER_LOG"]).write_text("")
    bad = subprocess.run(["bash", "update.sh"], cwd=repo, env={**env, "TEST_FAIL_BUILD": "yes"}, capture_output=True)
    assert bad.returncode != 0
    assert "up -d" not in Path(env["TEST_DOCKER_LOG"]).read_text()
    Path(env["TEST_RESTART_MARKER"]).unlink(missing_ok=True)
    Path(env["TEST_DOCKER_LOG"]).write_text("")
    bad = subprocess.run(["bash", "update.sh"], cwd=repo, env={**env, "TEST_FAIL_HEALTH": "yes"}, capture_output=True)
    assert bad.returncode != 0
    log = Path(env["TEST_DOCKER_LOG"]).read_text()
    assert "up -d --no-build --no-deps backend" in log
    assert "up -d --no-build --no-deps frontend" not in log
    Path(env["TEST_RESTART_MARKER"]).unlink(missing_ok=True)
    Path(env["TEST_DOCKER_LOG"]).write_text("")
    bad = subprocess.run(["bash", "update.sh"], cwd=repo, env={**env, "TEST_RUNTIME_DRIFT": "yes"}, capture_output=True)
    assert bad.returncode != 0
    assert "up -d --no-build --no-deps frontend" not in Path(env["TEST_DOCKER_LOG"]).read_text()
    Path(env["TEST_DOCKER_LOG"]).write_text("")
    bad = subprocess.run(["bash", "update.sh"], cwd=repo, env={**env, "TEST_FAIL_FRONTEND": "yes"}, capture_output=True)
    assert bad.returncode != 0
    assert b"UPDATE_OK" not in bad.stdout
    git("remote", "set-url", "origin", str(folder / "credential-secret-no-such-remote"))
    Path(env["TEST_DOCKER_LOG"]).write_text("")
    bad = subprocess.run(["bash", "update.sh"], cwd=repo, env=env, capture_output=True)
    assert bad.returncode != 0
    assert b"credential-secret" not in bad.stdout + bad.stderr
    assert Path(env["TEST_DOCKER_LOG"]).read_text() == ""
    git("remote", "set-url", "origin", str(repo))
    # Dirty tracked files stop before git, backup, or Docker actions.
    (repo / "backend/scripts/ensure_host_identity.sh").write_text("changed")
    Path(env["TEST_DOCKER_LOG"]).write_text("")
    bad = subprocess.run(["bash", "update.sh"], cwd=repo, env=env, capture_output=True)
    assert bad.returncode != 0
    assert Path(env["TEST_DOCKER_LOG"]).read_text() == ""

print("safe update: backup integrity, configured paths, operation refusals, exact target, runtime preservation and fake deploy order PASS")
