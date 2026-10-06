"""RV-60 / RV-65 (Receipt Void design, section 22 "AST gates"): nothing
outside services/wallet_service.py writes the customer wallet
(User.balance).

A write is any of:
  - an assignment or augmented assignment to an attribute named `balance`;
  - setattr(obj, "balance", ...);
  - .values(balance=...) or .values({"balance": ...}) on an UPDATE/INSERT;
  - a generic `setattr(obj, name, value)` loop in a function that handles a
    customer (`user`) without first taking "balance" out of the data.

AdminUser.balance (reseller credit) is a different system and is not the
target. The syntax tree cannot tell the two models apart by type, so the
reseller writers are listed by file and function, and a control proves the
list is still needed: every listed place must really contain a balance
write (a stale entry fails), and the scan must find it when the list is
empty.

RV-65 is the self-check: a made-up module with one violation of each kind
must be reported, one finding per violation.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
WRITER = "services/wallet_service.py"

# (file, function) pairs that write AdminUser.balance - reseller credit.
RESELLER_WRITERS = {
    ("routers/admins.py", "_apply_balance_change"),
    ("services/admin_billing.py", "debit_admin"),
    ("services/admin_billing.py", "refund_for_package"),
    ("services/reseller_refund_settlement.py", "settle"),
}

failures: list[str] = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def _is_balance_constant(node) -> bool:
    return isinstance(node, ast.Constant) and node.value == "balance"


def _functions(tree):
    """Every function with the nodes that belong to it (not to a nested one)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _owner_function(tree):
    """node id -> name of the innermost enclosing function ('<module>' if none)."""
    owner = {}

    def visit(node, name):
        for child in ast.iter_child_nodes(node):
            child_name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else name
            owner[id(child)] = name
            visit(child, child_name)

    visit(tree, "<module>")
    return owner


def balance_writes(source: str) -> list[tuple[str, int, str]]:
    """(function, line, kind) for every wallet-balance write in `source`."""
    tree = ast.parse(source)
    owner = _owner_function(tree)
    found = []
    for node in ast.walk(tree):
        where = owner.get(id(node), "<module>")
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            for part in ast.walk(target):
                if isinstance(part, ast.Attribute) and part.attr == "balance":
                    found.append((where, node.lineno, "assignment"))
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if name == "setattr" and len(node.args) >= 2 and _is_balance_constant(node.args[1]):
            found.append((where, node.lineno, "setattr"))
        if name == "values":
            in_keywords = any(keyword.arg == "balance" for keyword in node.keywords)
            in_dict = any(isinstance(arg, ast.Dict) and any(_is_balance_constant(key) for key in arg.keys)
                          for arg in node.args)
            if in_keywords or in_dict:
                found.append((where, node.lineno, "values"))
    # A generic setattr loop over a customer: the field name is not a
    # constant, so "balance" could flow through it unless it was removed.
    for function in _functions(tree):
        generic, removed = [], False
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if (name == "setattr" and len(node.args) >= 2 and not isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[0], ast.Name) and node.args[0].id == "user"):
                generic.append(node.lineno)
            if name == "pop" and node.args and _is_balance_constant(node.args[0]):
                removed = True
        if generic and not removed:
            found.extend((function.name, line, "generic setattr on user") for line in generic)
    return sorted(set(found))


def scan(allowed: set) -> tuple[list[str], set]:
    """Violations outside the writer, and which allow-list entries matched."""
    violations, matched = [], set()
    for path in sorted(APP.rglob("*.py")):
        relative = str(path.relative_to(APP))
        if relative == WRITER:
            continue
        for function, line, kind in balance_writes(path.read_text(encoding="utf-8")):
            if (relative, function) in allowed:
                matched.add((relative, function))
            else:
                violations.append(f"{relative}:{line} {function} ({kind})")
    return violations, matched


# ------------------------------------------------------------------ RV-60
violations, matched = scan(RESELLER_WRITERS)
check("RV-60 no module but wallet_service writes the customer wallet", violations, [])
check("every listed reseller-credit writer still writes a balance (no stale entry)", matched, RESELLER_WRITERS)
refund_source = (APP / "services/reseller_refund_settlement.py").read_text(encoding="utf-8")
refund_tree = ast.parse(refund_source)
refund_writes = balance_writes(refund_source)
refund_statements = [ast.get_source_segment(refund_source, node) for node in ast.walk(refund_tree)
                     if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "values"
                     and any(keyword.arg == "balance" for keyword in node.keywords)]
check("the refund exception is exactly one explicitly AdminUser UPDATE, never a User wallet write",
      ([(function, kind) for function, _line, kind in refund_writes],
       len(refund_statements),
       all("models.AdminUser.__table__.update()" in statement and
           "balance=models.AdminUser.balance + amount" in statement for statement in refund_statements)),
      ([("settle", "values")], 1, True))
unfiltered, _ = scan(set())
check("control: with the reseller list empty the scan reports those writers, so it does see balance writes",
      len(unfiltered) >= len(RESELLER_WRITERS), True)
writer_source = (APP / WRITER).read_text(encoding="utf-8")
check("the writer itself does write the balance (the gate is not vacuous)",
      sorted({kind for _function, _line, kind in balance_writes(writer_source)}), ["assignment", "values"])

# The column in the customer model is `balance` on `users`; if it is ever
# renamed this gate would pass while guarding nothing.
from app import models

check("the guarded column exists: users.balance", "balance" in models.User.__table__.c, True)

# ------------------------------------------------------------------ RV-65 self-check
FIXTURE = '''
def assign(user, amount):
    user.balance = amount

def augmented(user, amount):
    user.balance += amount

def by_name(user, amount):
    setattr(user, "balance", amount)

def statement(db, users, amount):
    db.execute(users.update().values(balance=users.c.balance + amount))

def statement_dict(db, users, amount):
    db.execute(users.update().values({"balance": amount}))

def generic(user, data):
    for key, value in data.items():
        setattr(user, key, value)

def generic_safe(user, data):
    data.pop("balance", None)
    for key, value in data.items():
        setattr(user, key, value)

def reads_only(user):
    return (user.balance or 0) + 1
'''
found = balance_writes(FIXTURE)
check("RV-65 one finding per violation, none for the safe loop or the read",
      [(function, kind) for function, _line, kind in found],
      [("assign", "assignment"), ("augmented", "assignment"), ("by_name", "setattr"),
       ("generic", "generic setattr on user"), ("statement", "values"), ("statement_dict", "values")])

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
