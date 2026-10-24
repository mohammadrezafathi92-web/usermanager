"""Node-contract versions are per backend, so an unrelated code change can
no longer invalidate every node (receipt 1140).

A contract records the version of the code that verified it. That version
used to be ONE hash of every adapter file, so splitting the WireGuard
cancellation helpers invalidated the 3X-UI and SoftEther contracts too and
blocked unrelated purchases. Now:

  * each backend's version hashes the shared contract code plus ITS OWN
    adapter only - a WireGuard-only change leaves every other backend's
    contracts valid; a shared-code change still (correctly) moves them all;
  * the cancellation/refund helpers live outside every hashed file, so a
    refund change invalidates no contract at all;
  * the current versions are PINNED below. A change to any hashed file fails
    this test and names exactly the backends whose contracts it would
    invalidate on deployment - review that before updating a pin (a fresh
    verification of those nodes, or a reviewed compatibility upgrade).

No network, no database.
"""
import ast
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app import models_provisioning as mp
from app.services import provisioning_contract_rules as rules, provisioning_contracts as contracts

failures = []


def check(label, got, expected=True):
    if callable(got):
        try:
            got = got()
        except Exception as exc:  # noqa: BLE001
            got = f"raised {type(exc).__name__}: {exc}"
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


SERVICES = Path(rules.__file__).parent
BACKENDS = mp.CONTRACT_BACKENDS
FAMILY = {"mikrotik_wg": "adapter_mikrotik_wg.py", "softether": "adapter_softether.py",
          "xray_ssh": "adapter_xray.py", "threexui": "adapter_xray.py",
          "marzban": "adapter_xray_panels.py", "hiddify": "adapter_xray_panels.py",
          "marzneshin": "adapter_xray_panels.py", "sui": "adapter_xray_panels.py"}
SHARED = ("adapter_base.py", "provisioning_contracts.py", "provisioning_contract_rules.py")
REFUND_HELPERS = ("StoppedUsage", "stop_and_measure", "remove_stopped", "stop_capture_remove")

# Update ONLY together with a review of which deployed contracts this
# invalidates (see the module docstring).
PINNED = {
    "mikrotik_wg": "4323bda8fa65dee12b9efb2d7802bb1bf3cdfed0",
    "softether": "0d948da339a557561f547c93676a929ba93fe0ca",
    "xray_ssh": "fb6bab0b9202b4924251bd73262dfe9689b9f244",
    "threexui": "fb6bab0b9202b4924251bd73262dfe9689b9f244",
    "marzban": "015d4adb2e9282e4fd88ef66f7a00a326016ec63",
    "hiddify": "015d4adb2e9282e4fd88ef66f7a00a326016ec63",
    "marzneshin": "015d4adb2e9282e4fd88ef66f7a00a326016ec63",
    "sui": "015d4adb2e9282e4fd88ef66f7a00a326016ec63",
}

print("--- the version is per backend ---")
check("the parent and the runner child share one version function",
      contracts.adapter_version is rules.adapter_version)
check("every contract backend has a 40-hex version",
      lambda: {b: len(rules.adapter_version(b)) == 40 and all(c in "0123456789abcdef" for c in rules.adapter_version(b))
               for b in BACKENDS}, {b: True for b in BACKENDS})
check("radius_ppp has no contract and therefore no version",
      lambda: rules.adapter_version("radius_ppp"), "raised ValueError: no node contract for backend 'radius_ppp'")
check("an unknown backend has no version",
      lambda: rules.adapter_version("made_up"), "raised ValueError: no node contract for backend 'made_up'")
check("the hashed files of each backend: shared contract code + its own adapter",
      lambda: {b: rules.contract_files(b) for b in BACKENDS}, {b: SHARED + (FAMILY[b],) for b in BACKENDS})
check("backends sharing one adapter module share its version, others differ",
      lambda: (rules.adapter_version("xray_ssh") == rules.adapter_version("threexui"),
               rules.adapter_version("marzban") == rules.adapter_version("sui"),
               len({rules.adapter_version(b) for b in ("mikrotik_wg", "softether", "threexui", "marzban")})),
      (True, True, 4))


def digests_after(edit_file):
    """Every backend's version computed on a scratch copy in which ONE hashed
    file got an appended comment (the real tree is never touched)."""
    with tempfile.TemporaryDirectory(prefix="um-contract-scope-") as scratch:
        for name in set(SHARED) | set(FAMILY.values()):
            shutil.copyfile(SERVICES / name, Path(scratch) / name)
        before = {b: rules.adapter_version(b, root=scratch) for b in BACKENDS}
        with open(Path(scratch) / edit_file, "a", encoding="utf-8") as fh:
            fh.write("\n# scope test edit\n")
        after = {b: rules.adapter_version(b, root=scratch) for b in BACKENDS}
        return sorted(b for b in BACKENDS if before[b] != after[b])


print("--- which change invalidates which contracts ---")
check("a WireGuard adapter change moves ONLY mikrotik_wg", lambda: digests_after("adapter_mikrotik_wg.py"),
      ["mikrotik_wg"])
check("a SoftEther adapter change moves ONLY softether", lambda: digests_after("adapter_softether.py"), ["softether"])
check("an Xray (SSH/3X-UI) adapter change moves only those two", lambda: digests_after("adapter_xray.py"),
      ["threexui", "xray_ssh"])
check("a panel adapter change moves only the four panel backends", lambda: digests_after("adapter_xray_panels.py"),
      ["hiddify", "marzban", "marzneshin", "sui"])
for name in SHARED:
    check(f"a shared contract-code change ({name}) still moves every backend", lambda name=name: digests_after(name),
          sorted(BACKENDS))
check("the scratch computation equals the real one on unchanged files",
      lambda: {b: rules.adapter_version(b, root=str(SERVICES)) == rules.adapter_version(b) for b in BACKENDS},
      {b: True for b in BACKENDS})

print("--- cancellation/refund code is outside every hashed file ---")
hashed = sorted(set(SHARED) | set(FAMILY.values()))
for name in hashed:
    tree = ast.parse((SERVICES / name).read_text(encoding="utf-8"))
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    check(f"{name} defines no refund helper", sorted(defined & set(REFUND_HELPERS)), [])
check("the refund helpers live in adapter_mikrotik_wg_refund.py",
      lambda: sorted(n for n in REFUND_HELPERS if hasattr(__import__("app.services.adapter_mikrotik_wg_refund",
                                                                     fromlist=["x"]), n)), sorted(REFUND_HELPERS))
check("adapter_mikrotik_wg_refund.py is not a hashed contract file",
      "adapter_mikrotik_wg_refund.py" in hashed, False)

print("--- deployment drift guard ---")
current = {}
for backend in BACKENDS:
    try:
        current[backend] = rules.adapter_version(backend)
    except Exception as exc:  # noqa: BLE001
        current[backend] = f"raised {type(exc).__name__}"
moved = sorted(b for b in BACKENDS if current[b] != PINNED[b])
check("no backend's contract version moved (else: these backends' deployed contracts are invalidated on "
      "deployment - re-verify those nodes or add a reviewed upgrade, then update PINNED)", moved, [])

if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
