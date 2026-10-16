"""RV-100 (Receipt Void section 22, G-NODELETE): no application code
deletes a financial or correlation row - the ledger, the receipt
approvals and their effects, or any wallet / loyalty table. A syntax scan:
a .delete() on one of those tables (Core) or a query of their model
(ORM), and db.delete() of a LedgerEntry. RV-100 self-check included.
"""
import ast
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = pathlib.Path(__file__).resolve().parent.parent / "app"
PROTECTED = {
    "receipt_approvals", "receipt_approval_effects", "receipt_approval_expected_effects", "wallet_operations",
    "wallet_operation_steps", "customer_identities", "wallet_accounts", "wallet_account_identity_rebinds",
    "wallet_credit_sources", "wallet_lots", "wallet_debit_events", "wallet_allocations",
    "wallet_allocation_reversals", "wallet_debts", "wallet_credit_applications",
    "wallet_credit_application_reversals", "wallet_debt_adjustments", "wallet_writeoffs",
    "wallet_cache_repair_events", "loyalty_policy_epochs", "loyalty_user_baselines", "sale_payment_evidence",
    "loyalty_purchase_events", "loyalty_reward_events", "quota_reversal_shortfalls",
}
failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def _names(node):
    """Every attribute/name in an expression chain, innermost first."""
    found = []
    while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
        if isinstance(node, ast.Attribute):
            found.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Call):
            found.extend(_arg_names(node))
            node = node.func
        else:
            node = node.value
    if isinstance(node, ast.Name):
        found.append(node.id)
    return found


def _arg_names(call):
    names = []
    for arg in call.args:
        if isinstance(arg, ast.Attribute):
            names.append(arg.attr)
        elif isinstance(arg, ast.Name):
            names.append(arg.id)
    return names


def deletes(source: str) -> list:
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "delete":
            continue
        chain = _names(node.func.value)
        if PROTECTED & set(chain) or "LedgerEntry" in chain or (
                chain[-1:] in (["db"], ["session"]) and any(
                    isinstance(a, ast.Name) and a.id in ("entry", "ledger") for a in node.args)):
            found.append(node.lineno)
    return found


violations = []
for path in sorted(APP.rglob("*.py")):
    for line in deletes(path.read_text(encoding="utf-8")):
        violations.append(f"{path.relative_to(APP)}:{line}")
check("RV-100 no application code deletes a financial or correlation row", violations, [])
sample = """
def bad(db):
    db.execute(rv.wallet_lots.delete().where(x))
    db.query(models.LedgerEntry).filter_by(id=1).delete()
    db.execute(rv.loyalty_reward_events.delete())
def fine(db):
    db.execute(rv.resource_locks.delete())
    db.query(models.Connection).delete()
"""
check("RV-100 self-check: one finding per protected delete, none for the others", len(deletes(sample)), 3)
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
