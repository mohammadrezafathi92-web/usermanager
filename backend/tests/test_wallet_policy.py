"""P5 policy separation; no cutover, network access or production data."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import models, models_receipt_void as rv
from app.services import wallet_policy as policy

engine = create_engine("sqlite://")
models.Base.metadata.create_all(engine)
rv.wallet_runtime_state.metadata.create_all(engine)
db = sessionmaker(bind=engine)()
failures = []


def check(label, actual, expected):
    if actual == expected:
        print("PASS", label)
    else:
        failures.append(label)
        print("FAIL", label, repr(actual), "!=", repr(expected))


def result(call):
    try:
        call()
        return "allowed"
    except HTTPException as exc:
        return (exc.status_code, exc.detail)


def phase(value):
    db.execute(rv.wallet_runtime_state.update().values(phase=value))
    db.commit()


first = models.User(username="policy_first", telegram_id=901, balance=250)
second = models.User(username="policy_second", telegram_id=901, balance=100)
other = models.User(username="policy_other", telegram_id=902, balance=50)
db.add_all([first, second, other])
db.commit()
db.execute(rv.wallet_runtime_state.insert().values(id=1, phase="normal"))
db.execute(rv.customer_identities.insert().values(
    id=1, tenant_scope_key="test", identity_key="telegram:901", telegram_id=901))
for user in (first, second):
    db.execute(rv.wallet_accounts.insert().values(
        id=user.id, lineage_key=f"00000000-0000-0000-0000-{user.id:012d}",
        customer_identity_id=1, user_id=user.id,
        user_id_snapshot=user.id, username_snapshot=user.username))
db.commit()

with patch.object(policy.receipt_void_schema, "is_ready", return_value=True):
    check("normal purchase allowed", result(lambda: policy.can_purchase(db, first)), "allowed")
    first.purchases_blocked = True
    first.purchases_blocked_reason = "administrator reason"
    db.commit()
    refused = (403, "administrator reason")
    check("own purchase lock", result(lambda: policy.can_purchase(db, first)), refused)
    check("second account cannot bypass lock", result(lambda: policy.can_purchase(db, second)), refused)
    check("new account cannot bypass lock", result(lambda: policy.can_purchase_telegram(db, 901)), refused)
    check("other customer unaffected", result(lambda: policy.can_purchase(db, other)), "allowed")
    check("normal top-up retains existing lock", result(lambda: policy.can_topup(db, first)), refused)
    check("normal top-up keeps old own-account scope", result(lambda: policy.can_topup(db, second)), "allowed")

    phase("enforced")
    check("fraud purchase lock does not forbid repayment", result(lambda: policy.can_topup(db, first)), "allowed")
    db.execute(rv.wallet_accounts.update().where(rv.wallet_accounts.c.user_id == first.id).values(topup_blocked=True))
    db.commit()
    check("explicit top-up block", result(lambda: policy.can_topup(db, first)), (403, "topup_blocked"))
    check("missing enforced account fails closed", result(lambda: policy.can_topup(db, other)), (503, "wallet_account_not_ready"))
    first.purchases_blocked = False
    db.commit()
    # SQLite does not enforce FKs in this project. This policy-only fixture
    # uses inert IDs for source references, not an actual void transaction.
    db.execute(rv.wallet_debts.insert().values(
        customer_identity_id=1, wallet_account_id=first.id,
        source_wallet_lot_id=1, source_void_operation_id=1,
        amount=100, adjusted_amount=10, settled_amount=20, state="partially_settled"))
    db.commit()
    check("identity debt blocks other account purchase", result(lambda: policy.can_purchase(db, second)), (409, "open_debt"))
    check("debt does not forbid repayment", result(lambda: policy.can_topup(db, second)), "allowed")
    db.execute(rv.wallet_debts.update().values(settled_amount=90, state="settled"))
    db.commit()
    check("settled debt permits purchase", result(lambda: policy.can_purchase(db, second)), "allowed")
    phase("fencing")
    check("fencing purchase fails closed", result(lambda: policy.can_purchase(db, second)), (503, "wallet_policy_unavailable"))
    check("fencing top-up fails closed", result(lambda: policy.can_topup(db, second)), (503, "wallet_policy_unavailable"))

with patch.object(policy.receipt_void_schema, "is_ready", return_value=False):
    check("pre-schema legacy availability", result(lambda: policy.can_purchase(db, other)), "allowed")

check("policy never changes balances", [first.balance, second.balance, other.balance], [250, 100, 50])
check("policy never writes effects", db.execute(rv.receipt_approval_effects.select()).fetchall(), [])
db.close()
engine.dispose()
sys.exit(bool(failures))
