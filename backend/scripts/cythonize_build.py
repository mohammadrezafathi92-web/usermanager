#!/usr/bin/env python3
"""Compiles app/services/*.py and app/telegram_bot/**/*.py into native .so
extensions via Cython, then deletes the .py sources that were compiled.

Runs ONLY inside the Docker 'compile' build stage (see backend/Dockerfile's
`compile` stage and the `runtime-compiled` target that copies its output) -
never on a developer's own checkout. It deletes source files; that is the
whole point here, but it would be destructive run anywhere else.

Scope is deliberately narrow - services/ and telegram_bot/, not everything:

  - app/services/* and app/telegram_bot/** are the actual business logic
    (quota/licence enforcement, RADIUS auth, hierarchy rules, the bot) -
    the part with real IP-theft/tamper value (licensing.verify() being
    patched to always return True is exactly the kind of thing this is
    meant to raise the bar against, per the panel owner's explicit ask:
    "یه راه نظارتی ... درصورت عدم پرداخت اشتراک دسترسی پنلشون رو ببندم").

  - app/routers/*, app/main.py, app/schemas.py, app/deps.py, app/models.py,
    app/config.py, app/database.py, app/permissions.py are DELIBERATELY
    left as plain .py. FastAPI resolves each endpoint's Depends()/Body()/
    Query() parameters by calling inspect.signature() on the route
    function; Cython-compiled functions can support that (this script
    compiles with `binding=True` specifically so they would), but it is a
    genuinely fragile corner where Cython and FastAPI meet, and these
    files are mostly thin CRUD wiring and table/schema declarations - low
    value to protect, catastrophic if compiling them broke routing on a
    live customer panel. Compiling only the high-value, low-risk subset
    keeps this change safe to ship.

  - __init__.py files are never compiled - Python needs a real file
    marking a directory as a package, and an __init__.py is rarely more
    than imports anyway, so there is nothing worth protecting there.

Fails LOUD on purpose: any file that does not produce a working .so aborts
the whole build (non-zero exit) rather than silently shipping an image
where that module silently fell back to interpreted source, or worse,
doesn't import at all. See main()'s post-compile verification pass.
"""
from __future__ import annotations

import pathlib
import sys

COMPILE_SUBDIRS = ("services", "telegram_bot")
EXCLUDE_NAMES = {"__init__.py"}


def find_targets(package_dir: pathlib.Path) -> list[pathlib.Path]:
    targets: list[pathlib.Path] = []
    for sub in COMPILE_SUBDIRS:
        base = package_dir / sub
        if not base.is_dir():
            continue
        for py in sorted(base.rglob("*.py")):
            if py.name in EXCLUDE_NAMES:
                continue
            targets.append(py)
    return targets


def module_name(package_dir: pathlib.Path, py: pathlib.Path) -> str:
    """app/services/foo.py -> app.services.foo (package_dir is .../app)."""
    rel = py.relative_to(package_dir).with_suffix("")
    return ".".join((package_dir.name, *rel.parts))


def compile_all(package_dir: pathlib.Path, targets: list[pathlib.Path]) -> int:
    from Cython.Build import cythonize
    from setuptools import setup
    from setuptools.extension import Extension

    extensions = [
        Extension(name=module_name(package_dir, py), sources=[str(py)])
        for py in targets
    ]

    compiled = cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            # Preserves inspect.signature()/__defaults__/__annotations__ on
            # the compiled functions - not needed by anything in
            # services/telegram_bot today, but cheap insurance against a
            # future caller that DOES introspect one of them (e.g. a new
            # dependency-injection helper), and it costs nothing at runtime.
            "binding": True,
        },
        build_dir=str(package_dir.parent / "_cython_build"),
        force=True,
        # This script's own Docker log IS the only place these warnings/
        # errors are ever seen (no interactive terminal in a build) - more
        # context beats a bare exception at the docker build step.
        show_all_warnings=True,
    )

    # setup()'s sys.exit(0)-on-success / raises-on-failure behaviour is
    # exactly what a build script wants - a failed compile must abort the
    # image build, not produce a half-built layer.
    setup(
        name="usermanager-compiled",
        ext_modules=compiled,
        script_args=["build_ext", "--inplace"],
    )
    return len(compiled)


def verify_and_clean(package_dir: pathlib.Path, targets: list[pathlib.Path]) -> int:
    """Confirms every target actually produced a .so RIGHT NEXT TO its
    source (build_ext --inplace's placement, which is what makes it
    importable as app.services.foo with no path changes needed) before
    deleting anything. Returns 0 and removes the .py + any stale
    __pycache__ .pyc for it; returns 1 and removes nothing further the
    moment one file is missing its .so - a partially-cleaned tree would be
    a worse failure mode than an obviously-incomplete build."""
    missing = []
    for py in targets:
        so_hits = list(py.parent.glob(py.stem + ".*.so")) + list(py.parent.glob(py.stem + ".so"))
        if not so_hits:
            missing.append(py)
    if missing:
        for py in missing:
            print(f"cythonize_build: {py} کامپایل نشد - فایل .so پیدا نشد", file=sys.stderr)
        return 1

    for py in targets:
        py.unlink()
        pycache = py.parent / "__pycache__"
        if pycache.is_dir():
            for stale in pycache.glob(py.stem + ".*.pyc"):
                stale.unlink()

    print(f"cythonize_build: {len(targets)} فایل با موفقیت کامپایل و منبع آن حذف شد")
    return 0


def main(workdir: pathlib.Path) -> int:
    package_dir = workdir / "app"
    if not package_dir.is_dir():
        print(f"cythonize_build: پوشه‌ی {package_dir} پیدا نشد", file=sys.stderr)
        return 1

    targets = find_targets(package_dir)
    if not targets:
        print("cythonize_build: هیچ فایلی برای کامپایل پیدا نشد - چیزی درست نیست", file=sys.stderr)
        return 1

    compile_all(package_dir, targets)
    return verify_and_clean(package_dir, targets)


if __name__ == "__main__":
    base = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path("/app")
    sys.exit(main(base))
