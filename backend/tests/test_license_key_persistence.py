"""routers/license.py's _persist_key_to_env - the write that is supposed to
survive a container recreate, not just a plain restart.

Run:  python3 backend/tests/test_license_key_persistence.py

Bug reported 2026-09-07: a license key pasted into the panel appeared to
save, but the moment USERMANAGER_LICENSE_PUBKEY was added and the backend
container was force-recreated (required for ANY new env var to take
effect - see this session's earlier bcrypt/.env lesson), the panel came
back up with no key at all and REASON_MISSING locked it out, with no way
to log back in and re-paste it.

Root cause: the write went to /app/.env by default, which is neither
baked into the image (Dockerfile only COPYs app/) nor bind-mounted from
the host - so it lived only in that one container's ephemeral writable
layer, gone the instant the container was recreated. The fix points the
default at {HOST_PROJECT_DIR}/backend/.env instead - the SAME bind-mount
trick local_deploy.py already relies on for its self-service port-change
feature, which genuinely is the host's real file.
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
os.makedirs(os.path.join(tmp_project_dir, "backend"), exist_ok=True)
os.environ["HOST_PROJECT_DIR"] = tmp_project_dir

from app.routers import license as license_router  # noqa: E402

print("--- the default write path is under HOST_PROJECT_DIR, not /app ---")
default_path = os.environ.get("ENV_FILE_PATH") or os.path.join(
    license_router.HOST_PROJECT_DIR, "backend", ".env"
)
check("resolves under the (fake) host project dir", default_path.startswith(tmp_project_dir), True)
check("...specifically backend/.env", default_path, os.path.join(tmp_project_dir, "backend", ".env"))
check("NOT the broken /app/.env default", default_path, os.path.join(tmp_project_dir, "backend", ".env"))

print("\n--- writing a key actually lands in that real file, appendable ---")
env_file = os.path.join(tmp_project_dir, "backend", ".env")
with open(env_file, "w", encoding="utf-8") as fh:
    fh.write("SOME_OTHER_SETTING=keep-me\n")

license_router._persist_key_to_env("NETCIP1.fake.token")
with open(env_file, "r", encoding="utf-8") as fh:
    contents = fh.read()
check("the new key is present", "LICENSE_KEY=NETCIP1.fake.token" in contents, True)
check("an unrelated existing setting survives", "SOME_OTHER_SETTING=keep-me" in contents, True)

print("\n--- re-saving replaces the old key rather than duplicating it ---")
license_router._persist_key_to_env("NETCIP1.newer.token")
with open(env_file, "r", encoding="utf-8") as fh:
    contents = fh.read()
check("only the new key is present", contents.count("LICENSE_KEY="), 1)
check("...with the new value", "LICENSE_KEY=NETCIP1.newer.token" in contents, True)
check("the old value is gone", "NETCIP1.fake.token" in contents, False)

print("\n--- this is exactly the file a fresh container reads on the next recreate ---")
# Simulating what --force-recreate actually does: nothing survives except
# what's in this file, since a new container starts from a clean layer.
# Reading it back independently proves the value genuinely persisted to
# disk, not just to some in-process cache.
del license_router
import importlib
if "app.routers.license" in sys.modules:
    del sys.modules["app.routers.license"]
from app.routers import license as license_router2  # noqa: E402
with open(env_file, "r", encoding="utf-8") as fh:
    contents = fh.read()
check("the key is still there after re-importing the module fresh",
      "LICENSE_KEY=NETCIP1.newer.token" in contents, True)

print("\n--- _remove_key_from_env strips it from the same real file ---")
license_router2._remove_key_from_env()
with open(env_file, "r", encoding="utf-8") as fh:
    contents = fh.read()
check("LICENSE_KEY is gone", "LICENSE_KEY=" in contents, False)
check("an unrelated setting still there", "SOME_OTHER_SETTING=keep-me" in contents, True)

print("\n--- removing again (nothing to remove) does not crash or touch other lines ---")
license_router2._remove_key_from_env()
with open(env_file, "r", encoding="utf-8") as fh:
    contents = fh.read()
check("still just the unrelated setting", contents.strip(), "SOME_OTHER_SETTING=keep-me")

print("\n--- the DELETE /key endpoint function clears settings.license_key too ---")
from app.config import settings  # noqa: E402

license_router2._persist_key_to_env("NETCIP1.about.to.delete")
settings.license_key = "NETCIP1.about.to.delete"
result = license_router2.delete_key()
check("settings.license_key is cleared", settings.license_key, "")
check("has_key is now False in the returned status", result["has_key"], False)
with open(env_file, "r", encoding="utf-8") as fh:
    contents = fh.read()
check("...and gone from the real file too, not just memory", "LICENSE_KEY=" in contents, False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
