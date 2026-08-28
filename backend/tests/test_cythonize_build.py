"""scripts/cythonize_build.py - the build-time script that compiles
app/services/ and app/telegram_bot/ into .so extensions for the optional
`runtime-compiled` Docker target (see backend/Dockerfile and
docker-compose.yml's BACKEND_BUILD_TARGET). Never imported by the running
app itself - only by `docker build` inside the `compile` stage - so this
test loads it directly from its file path rather than as a package module.

Covers, on throwaway temp directories (never the real backend/app/ - this
script DELETES .py files after compiling, so it must never run against a
real checkout):
  - find_targets(): only services/+telegram_bot/, __init__.py excluded,
    nested subpackages (handlers/) picked up, sibling dirs (routers/) and
    top-level files (main.py) ignored.
  - module_name(): correct dotted app.<pkg>.<mod> path, including nested.
  - verify_and_clean(): refuses to delete anything if even one target is
    missing its .so; deletes cleanly (py + stale __pycache__) when all
    targets compiled.
  - main(): missing package dir and "nothing to compile" both fail loud
    (exit 1) rather than silently doing nothing.
  - a REAL end-to-end compile on a tiny 2-file fake package, using the
    actual Cython+gcc toolchain - proves the whole pipeline (not just the
    file-selection logic) produces an importable, correctly-behaving .so
    and removes exactly the right .py files. Skipped with a clear message
    if cython/gcc are not installed on this machine (they are guaranteed
    inside the Docker `compile` stage; not guaranteed on every dev box
    this test might run on).

Run:  python3 backend/tests/test_cythonize_build.py
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(HERE)
sys.path.insert(0, BACKEND_DIR)

SCRIPT_PATH = os.path.join(BACKEND_DIR, "scripts", "cythonize_build.py")
_spec = importlib.util.spec_from_file_location("cythonize_build", SCRIPT_PATH)
cb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cb)

import pathlib

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def make_fake_package(root: pathlib.Path) -> pathlib.Path:
    """A tiny stand-in for backend/app/ - services/, telegram_bot/ (with a
    nested handlers/ subpackage), a routers/ sibling that must be ignored,
    and a top-level main.py that must also be ignored."""
    pkg = root / "app"
    (pkg / "services").mkdir(parents=True)
    (pkg / "telegram_bot" / "handlers").mkdir(parents=True)
    (pkg / "routers").mkdir(parents=True)

    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text("VALUE = 'main'\n")

    (pkg / "services" / "__init__.py").write_text("")
    (pkg / "services" / "greet.py").write_text(
        "def greet(name):\n    return 'hello ' + name\n"
    )

    (pkg / "telegram_bot" / "__init__.py").write_text("")
    (pkg / "telegram_bot" / "handlers" / "__init__.py").write_text("")
    (pkg / "telegram_bot" / "handlers" / "start.py").write_text(
        "GREETING = 'salam'\n"
    )

    (pkg / "routers" / "__init__.py").write_text("")
    (pkg / "routers" / "users.py").write_text("ROUTE = '/users'\n")

    return pkg


print("--- find_targets(): only services/+telegram_bot/, __init__.py excluded ---")
with tempfile.TemporaryDirectory() as tmp:
    pkg = make_fake_package(pathlib.Path(tmp))
    targets = {str(p.relative_to(pkg)) for p in cb.find_targets(pkg)}
    check("exactly the two real modules, nothing from routers/ or main.py",
          targets,
          {os.path.join("services", "greet.py"),
           os.path.join("telegram_bot", "handlers", "start.py")})

print("\n--- find_targets(): missing subdirs are just skipped, not an error ---")
with tempfile.TemporaryDirectory() as tmp:
    pkg = pathlib.Path(tmp) / "app"
    (pkg / "services").mkdir(parents=True)
    (pkg / "services" / "only_one.py").write_text("X = 1\n")
    targets = cb.find_targets(pkg)
    check("no telegram_bot/ dir at all -> just the one services/ file",
          [str(p.relative_to(pkg)) for p in targets],
          [os.path.join("services", "only_one.py")])

print("\n--- module_name(): dotted path, including nested subpackages ---")
pkg = pathlib.Path("/x/app")
check("top-level services module",
      cb.module_name(pkg, pkg / "services" / "greet.py"),
      "app.services.greet")
check("nested telegram_bot.handlers module",
      cb.module_name(pkg, pkg / "telegram_bot" / "handlers" / "start.py"),
      "app.telegram_bot.handlers.start")

print("\n--- verify_and_clean(): refuses to delete anything if one .so is missing ---")
with tempfile.TemporaryDirectory() as tmp:
    pkg = make_fake_package(pathlib.Path(tmp))
    targets = cb.find_targets(pkg)
    # Simulate only ONE of the two having actually compiled.
    greet = pkg / "services" / "greet.py"
    (greet.parent / "greet.cpython-311-x86_64-linux-gnu.so").write_bytes(b"\x00")
    rc = cb.verify_and_clean(pkg, targets)
    check("reports failure", rc, 1)
    check("the one WITH a .so is still left alone (all-or-nothing)",
          greet.exists(), True)
    start = pkg / "telegram_bot" / "handlers" / "start.py"
    check("the one WITHOUT a .so obviously still exists too",
          start.exists(), True)

print("\n--- verify_and_clean(): deletes cleanly when every target compiled ---")
with tempfile.TemporaryDirectory() as tmp:
    pkg = make_fake_package(pathlib.Path(tmp))
    targets = cb.find_targets(pkg)
    for py in targets:
        (py.parent / f"{py.stem}.cpython-311-x86_64-linux-gnu.so").write_bytes(b"\x00")
        pycache = py.parent / "__pycache__"
        pycache.mkdir(exist_ok=True)
        (pycache / f"{py.stem}.cpython-311.pyc").write_bytes(b"\x00")
    rc = cb.verify_and_clean(pkg, targets)
    check("reports success", rc, 0)
    check("both .py sources are gone",
          all(not py.exists() for py in targets), True)
    check("stale __pycache__ .pyc for them is gone too",
          not list((pkg / "services" / "__pycache__").glob("greet.*.pyc")), True)
    check("__init__.py files were never touched",
          (pkg / "services" / "__init__.py").exists(), True)
    check("routers/ and main.py were never touched",
          (pkg / "routers" / "users.py").exists() and (pkg / "main.py").exists(), True)

print("\n--- main(): fails loud, not silently, on bad input ---")
with tempfile.TemporaryDirectory() as tmp:
    rc = cb.main(pathlib.Path(tmp))  # no app/ subdir at all
    check("missing package dir -> exit 1", rc, 1)

with tempfile.TemporaryDirectory() as tmp:
    pkg = pathlib.Path(tmp) / "app"
    (pkg / "services").mkdir(parents=True)
    (pkg / "services" / "__init__.py").write_text("")  # only an __init__, nothing compilable
    rc = cb.main(pathlib.Path(tmp))
    check("nothing to compile -> exit 1 (not a silent no-op)", rc, 1)


def _toolchain_available() -> bool:
    if shutil.which("gcc") is None and shutil.which("cc") is None:
        return False
    try:
        import Cython  # noqa: F401
    except ImportError:
        return False
    return True


print("\n--- end-to-end: a real compile on a tiny fake package ---")
if not _toolchain_available():
    print("SKIP  cython/gcc not available on this machine - covered instead by "
          "find_targets/module_name/verify_and_clean above, which is the same "
          "logic main() drives; the real toolchain is only guaranteed inside "
          "backend/Dockerfile's `compile` stage")
else:
    with tempfile.TemporaryDirectory() as tmp:
        pkg = make_fake_package(pathlib.Path(tmp))
        proc = subprocess.run(
            [sys.executable, SCRIPT_PATH, tmp],
            capture_output=True, text=True, timeout=120,
        )
        check("the script itself exits 0", proc.returncode, 0)

        greet_py = pkg / "services" / "greet.py"
        start_py = pkg / "telegram_bot" / "handlers" / "start.py"
        check("greet.py source was removed", greet_py.exists(), False)
        check("start.py source was removed", start_py.exists(), False)
        check("__init__.py files were left as plain .py (never compiled)",
              (pkg / "services" / "__init__.py").exists(), True)
        check("main.py (outside the compile scope) untouched",
              (pkg / "main.py").read_text(), "VALUE = 'main'\n")

        so_files = list((pkg / "services").glob("greet.*.so"))
        check("a real .so landed next to where greet.py was", len(so_files) == 1, True)

        # Prove it doesn't just exist - it actually works, imported as the
        # real module name main() computed (app.services.greet).
        sys.path.insert(0, tmp)
        try:
            greet_mod = importlib.import_module("app.services.greet")
            check("compiled module is importable under its real dotted name",
                  greet_mod.__file__.endswith(".so"), True)
            check("...and the compiled function behaves identically to the source",
                  greet_mod.greet("dunya"), "hello dunya")
        finally:
            sys.path.remove(tmp)
            for mod_name in list(sys.modules):
                if mod_name == "app" or mod_name.startswith("app."):
                    del sys.modules[mod_name]

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
