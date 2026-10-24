"""Owner-approved unpaid reseller cancellation refund: MAX consumption.

consumed = max(elapsed/duration, used/quota) over the FINITE dimensions,
each clamped to [0, 1]; an unlimited dimension is omitted; both unlimited
refuses automatic proration. refund = floor(actual debited seller-credit
amount * (1 - consumed)), exact Fractions, one final round-down. Never the
retail sale or the current package price. Not receipt-fraud void / bank refund.

Synthetic local SQLite only, network blocked. UM_REVIEW_SOURCE_ROOT runs it
against an immutable pre-fix backend snapshot.
"""
import datetime as dt
import os
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

ROOT = Path(os.environ.get("UM_REVIEW_SOURCE_ROOT", str(Path(__file__).resolve().parents[1])))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-max-proration-only-0123456789abcdef0123")
os.environ.setdefault("RADIUS_ENABLED", "false")
import _no_network
_no_network.install()

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app import models
from app.config import settings
from app.database import Base
from app.models_provisioning import ProvisioningOperation
from app.models_reseller_refund import ResellerRefundOperation as Operation, ResellerSaleBasis
from app.services import reseller_refund_settlement as settlement
from app.services import reseller_refund_worker as worker
from app.services.reseller_refund import bind_sale, calculate_refund, preview

failures = []
DAY = 86400


def check(name, actual, expected):
    print("PASS" if actual == expected else "FAIL", name, actual if actual == expected else
          f"got {actual!r}, expected {expected!r}")
    if actual != expected:
        failures.append(name)


def refund(amount, used, quota, elapsed, duration):
    return calculate_refund(amount, used_bytes=used, quota_bytes=quota,
                            elapsed_seconds=elapsed, duration_seconds=duration)["refund_amount"]


def refused(fn):
    try:
        fn()
    except (ValueError, HTTPException) as exc:
        return getattr(exc, "detail", "ValueError")
    return None


# ---------------------------------------------------------------- arithmetic
# Owner examples, amount 200000.
check("t10% q50% -> 100000", refund(200000, 50, 100, 1 * DAY, 10 * DAY), 100000)
check("t80% q20% -> 40000", refund(200000, 20, 100, 8 * DAY, 10 * DAY), 40000)
check("volume exhausted, time 10% -> 0", refund(200000, 100, 100, 1 * DAY, 10 * DAY), 0)
check("time exhausted, volume 0% -> 0", refund(200000, 0, 100, 10 * DAY, 10 * DAY), 0)
check("volume over quota clamps to 0 refund", refund(200000, 10**12, 100, 0, 10 * DAY), 0)
check("time past expiry clamps to 0 refund", refund(200000, 0, 100, 10**9, 10 * DAY), 0)
check("nothing consumed refunds everything", refund(200000, 0, 100, 0, 10 * DAY), 200000)
result = calculate_refund(200000, used_bytes=50, quota_bytes=100, elapsed_seconds=DAY, duration_seconds=10 * DAY)
check("consumed percent is the max dimension", result["consumed_percent"], 50.0)
check("consumed amount is the complement", result["consumed_amount"], 100000)
check("dimension percents still reported", (result["time_percent"], result["quota_percent"]), (10.0, 50.0))

# Asymmetric volume/time and symmetry of the two axes.
check("volume-dominant asymmetric", refund(1000, 7, 10, 3, 10), 300)
check("time-dominant asymmetric", refund(1000, 3, 10, 7, 10), 300)
for t, q in ((1, 9), (9, 1), (2, 5), (5, 2), (0, 10), (10, 0)):
    check(f"axis symmetry t={t}/10 q={q}/10", refund(10**6, q, 10, t, 10), refund(10**6, t, 10, q, 10))

# Unlimited dimensions are omitted, never counted as zero consumption.
check("unlimited quota -> time only", refund(200000, 10**15, 0, 25, 100), 150000)
check("unlimited quota ignores any usage", refund(200000, 0, 0, 25, 100), 150000)
check("unlimited time -> volume only", refund(200000, 30, 100, 10**9, 0), 140000)
check("unlimited time ignores elapsed", refund(200000, 30, 100, 0, 0), 140000)
check("both unlimited refuses automatic proration",
      refused(lambda: refund(200000, 5, 0, 5, 0)), "ValueError")

# Exact Fraction arithmetic, one final round-down.
huge = 10**18 + 7
check("huge exact amount floor", refund(huge, 1, 3, 1, 7), (huge * 2) // 3)
check("fraction floor once (200/3)", refund(100, 1, 3, 0, 9), 66)
check("no float drift at 1/10", refund(10, 1, 10, 0, 10), 9)
amount, quota, duration = 999983, 7919, 104729
check("exact floor on prime operands", refund(amount, 2000, quota, 5000, duration),
      (amount * (1 - max(Fraction(2000, quota), Fraction(5000, duration)))).__floor__())

# Monotonic, bounded, and refund + consumed == charged.
previous = 123457
for used in range(0, 121, 7):
    value = refund(123457, used, 100, 13, 100)
    check(f"monotonic in volume at {used}", value <= previous, True)    # every step asserted
    previous = value
previous = 123457
for elapsed in range(0, 121, 9):
    value = refund(123457, 13, 100, elapsed, 100)
    check(f"monotonic in time at {elapsed}", value <= previous, True)
    previous = value
for used, elapsed in ((0, 0), (37, 61), (100, 0), (0, 100), (250, 250)):
    r = calculate_refund(123457, used_bytes=used, quota_bytes=100, elapsed_seconds=elapsed, duration_seconds=100)
    check(f"bounds and sum used={used} elapsed={elapsed}",
          (0 <= r["refund_amount"] <= 123457, r["refund_amount"] + r["consumed_amount"]), (True, 123457))
zero = calculate_refund(0, used_bytes=5, quota_bytes=10, elapsed_seconds=1, duration_seconds=10)
check("zero amount refunds zero", (zero["refund_amount"], zero["consumed_amount"]), (0, 0))
VALID = {"amount": 100, "used_bytes": 1, "quota_bytes": 10, "elapsed_seconds": 1, "duration_seconds": 10}


def call(**values):
    args = dict(VALID, **values)
    return calculate_refund(args.pop("amount"), **args)


check("valid baseline accepted", call()["refund_amount"], 90)
for field in VALID:
    for label, bad in (("negative", -1), ("boolean True", True), ("boolean False", False), ("float", 1.0),
                       ("fraction", Fraction(1, 2)), ("text", "1"), ("None", None)):
        check(f"{field}: {label} refused", refused(lambda: call(**{field: bad})), "ValueError")


# ------------------------------------------------- preview: actual debit basis
def fixture(folder, name, *, quota_bytes=1000, duration_days=10, debit_amount=100000):
    engine = create_engine(f"sqlite:///{folder}/{name}.db")
    Base.metadata.create_all(engine)
    ProvisioningOperation.__table__.create(engine)
    db = Session(engine)
    actor = models.AdminUser(username="seller", hashed_password="unused", role="seller", tree_path="/1/", balance=0)
    node = models.Node(name="synthetic", type=models.NodeType.mikrotik, mt_host="10.20.0.1")
    package = models.Package(name="synthetic", price=150000, duration_days=duration_days, quota_gb=1)
    db.add_all([actor, node, package]); db.flush()
    user = models.User(username="synthetic", owner_admin_id=actor.id, status=models.UserStatus.active)
    db.add(user); db.flush()
    expire = (dt.datetime(2030, 1, 11, 0, 0, 0) if duration_days else None)
    purchase = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=quota_bytes,
                               used_bytes=0, expire_at=expire)
    db.add(purchase); db.flush()
    connection = models.Connection(user_id=user.id, purchase_id=purchase.id, node_id=node.id,
                                   type=models.ConnectionType.openvpn, ppp_username="synthetic-ovpn",
                                   ppp_password="unused")
    db.add(connection); db.flush()
    debit = models.LedgerEntry(kind="admin_credit_spend", amount=debit_amount, admin_id=actor.id)
    sale = models.LedgerEntry(kind="sale_new", amount=180000, admin_id=actor.id,
                              user_id=user.id, purchase_id=purchase.id)
    db.add_all([debit, sale]); db.flush()
    bind_sale(db, purchase, debit, package, sale_entry=sale)
    db.commit()
    return engine, db, actor.id, package.id, purchase.id, connection.id


def use(db, purchase_id, connection_id, used):
    db.get(models.Connection, connection_id).total_bytes = used
    db.get(models.Purchase, purchase_id).used_bytes = used
    db.commit()


def snapshot(db):
    return (db.query(models.LedgerEntry).count(), db.query(ResellerSaleBasis).count(),
            [(b.charged_amount, b.quota_bytes, b.duration_seconds) for b in db.query(ResellerSaleBasis)])


START = dt.datetime(2030, 1, 1, 0, 0, 0)   # expire 2030-01-11 minus 10 days

with tempfile.TemporaryDirectory(prefix="um-max-proration-") as folder:
    engine, db, actor_id, package_id, pid, cid = fixture(folder, "preview")
    use(db, pid, cid, 500)                                   # 50% volume
    db.get(models.Package, package_id).price = 999999        # later price change
    db.commit()
    before = snapshot(db)
    shown = preview(db, db.get(models.Purchase, pid), now=START + dt.timedelta(days=1))   # 10% time
    check("preview uses max consumption", shown["refund_amount"], 50000)
    check("preview basis is the actual debit, not price or retail", shown["charged_amount"], 100000)
    check("preview writes nothing", (snapshot(db), bool(db.new or db.dirty or db.deleted)), (before, False))
    shown = preview(db, db.get(models.Purchase, pid), now=START + dt.timedelta(days=8))   # 80% time
    check("preview time-dominant", shown["refund_amount"], 20000)
    shown = preview(db, db.get(models.Purchase, pid), now=START + dt.timedelta(days=30))
    check("preview after expiry refunds nothing", shown["refund_amount"], 0)
    db.close(); engine.dispose()

    engine, db, _a, _p, pid, cid = fixture(folder, "unlimited_quota", quota_bytes=0)
    use(db, pid, cid, 10**12)
    shown = preview(db, db.get(models.Purchase, pid), now=START + dt.timedelta(days=4))
    check("preview unlimited quota is time only", shown["refund_amount"], 60000)
    db.close(); engine.dispose()

    engine, db, _a, _p, pid, cid = fixture(folder, "unlimited_time", duration_days=0)
    use(db, pid, cid, 300)
    shown = preview(db, db.get(models.Purchase, pid), now=dt.datetime(2099, 1, 1))
    check("preview unlimited time is volume only", shown["refund_amount"], 70000)
    db.close(); engine.dispose()

    engine, db, _a, _p, pid, cid = fixture(folder, "unmetered", quota_bytes=0, duration_days=0)
    check("preview both unlimited refuses",
          refused(lambda: preview(db, db.get(models.Purchase, pid))), "reseller_proration_unavailable")
    db.close(); engine.dispose()

    # ------------------------------------- final settle == preview, replay
    with patch.dict(os.environ, {"UM_LOCK_DIR": folder}), \
            patch.object(settings, "reseller_cancellation_enabled", True):
        engine, db, actor_id, package_id, pid, cid = fixture(folder, "settle")
        db.get(models.Purchase, pid).expire_at = basis_expire = dt.datetime.utcnow() + dt.timedelta(days=10)
        db.query(ResellerSaleBasis).filter_by(purchase_id=pid).one().expire_at = basis_expire
        db.commit()
        use(db, pid, cid, 500)                       # 50% volume, time ~0%
        db.get(models.Package, package_id).price = 1
        db.commit()
        estimate = preview(db, db.get(models.Purchase, pid))["refund_amount"]
        opid = settlement.prepare(db, purchase_id=pid, actor=db.get(models.AdminUser, actor_id))
        worker._start_mutation(db, opid)
        settlement.block_ppp(db, operation_id=opid, connection_id=cid)
        settlement.capture_ppp(db, operation_id=opid, connection_id=cid)
        settlement.verify_stopped(db, operation_id=opid, connection_id=cid, final_total_bytes=500)
        final = settlement.settle(db, operation_id=opid)
        check("final settle uses max consumption on actual debit", final["refund_amount"], 50000)
        check("final settle equals preview of the same usage", final["refund_amount"], estimate)
        refunds = db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund").all()
        check("one refund entry of the settled amount", [e.amount for e in refunds], [50000])
        check("completed replay returns stored result unchanged", settlement.settle(db, operation_id=opid), final)
        check("replay moves no further money",
              db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund").count(), 1)
        db.close(); engine.dispose()

        # ------------------- HISTORICAL completed operation (old mean formula)
        # Synthetic model of an operation that really completed BEFORE this
        # change: the real settle() runs with the previous calculator (mean
        # of finite dimensions), so its Operation, refund ledger entry and
        # seller balance are the genuine writes of that code. Same usage now
        # gives max 50000; the stored 75000 must be returned, never redone.
        def mean_calculator(amount, *, used_bytes, quota_bytes, elapsed_seconds, duration_seconds):
            dims = [min(Fraction(elapsed_seconds, duration_seconds), Fraction(1))] if duration_seconds else []
            dims += [min(Fraction(used_bytes, quota_bytes), Fraction(1))] if quota_bytes else []
            consumed = sum(dims, Fraction(0)) / len(dims)
            remainder = amount * (1 - consumed)
            value = remainder.numerator // remainder.denominator
            return {"charged_amount": amount, "refund_amount": value, "consumed_amount": amount - value,
                    "time_percent": None, "quota_percent": None, "consumed_percent": float(consumed * 100)}

        from app.services import reseller_refund as calculator_module
        engine, db, actor_id, package_id, pid, cid = fixture(folder, "historical")
        # period starts an hour from now: elapsed is exactly 0 however slow the run
        expire = dt.datetime.utcnow() + dt.timedelta(days=10, hours=1)
        db.get(models.Purchase, pid).expire_at = expire
        db.query(ResellerSaleBasis).filter_by(purchase_id=pid).one().expire_at = expire
        db.commit()
        use(db, pid, cid, 500)                       # 50% volume, time ~0%
        old_key = db.get(models.Purchase, pid).history_key
        with patch.object(calculator_module, "calculate_refund", mean_calculator):
            opid = settlement.prepare(db, purchase_id=pid, actor=db.get(models.AdminUser, actor_id))
            worker._start_mutation(db, opid)
            settlement.block_ppp(db, operation_id=opid, connection_id=cid)
            settlement.capture_ppp(db, operation_id=opid, connection_id=cid)
            settlement.verify_stopped(db, operation_id=opid, connection_id=cid, final_total_bytes=500)
            historical = settlement.settle(db, operation_id=opid)
        check("historical fixture really completed under the old mean", (historical["state"],
              historical["refund_amount"]), ("completed", 75000))
        check("new formula would give a different amount for the same usage",
              calculate_refund(100000, used_bytes=500, quota_bytes=1000, elapsed_seconds=0,
                               duration_seconds=10 * DAY)["refund_amount"], 50000)
        db.expire_all()
        ledger_before = sorted((e.kind, e.amount) for e in db.query(models.LedgerEntry))
        balance_before = db.get(models.AdminUser, actor_id).balance

        def must_not_recalculate(*_a, **_k):
            raise AssertionError("completed operation was recalculated")

        # every name the completed-replay path could reach the calculator through
        with patch.multiple(calculator_module, calculate_refund=must_not_recalculate, preview=must_not_recalculate), \
                patch.object(settlement, "preview", must_not_recalculate), \
                patch.object(worker, "preview", must_not_recalculate), \
                patch.object(worker.sys, "platform", "linux"):
            replay = outcome = None
            try:
                replay = settlement.settle(db, operation_id=opid)
                outcome = worker.execute(db, purchase_id=pid, actor=db.get(models.AdminUser, actor_id),
                                         purchase_key=old_key)
            except (AssertionError, HTTPException) as exc:
                outcome = f"raised {exc!r}"
        check("historical retry returns the stored 75000 unchanged", replay, historical)
        check("historical worker retry (service key) returns the stored result", outcome, historical)
        db.expire_all()
        check("historical retry writes no ledger entry",
              sorted((e.kind, e.amount) for e in db.query(models.LedgerEntry)), ledger_before)
        check("historical retry leaves the seller balance", db.get(models.AdminUser, actor_id).balance,
              balance_before)
        check("historical balance is the stored refund", balance_before, 75000)
        check("historical stored amount untouched", db.get(Operation, opid).refund_amount, 75000)
        db.close(); engine.dispose()

if failures:
    raise SystemExit(f"{len(failures)} FAILED")
print("all max-proration checks passed")
