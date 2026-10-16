"""Receipt Void 12.5 (R2), 12.6 (V), 14.1 (quota reversal) arithmetic on the
lot wallet, on SQLite AND real MariaDB (CI). No network.

  V     a scam top-up half spent: the rest clawed back, the spent part a debt
        of the same identity; fully spent; never spent; spent by a third
        party (referral) -> a write-off, not a debt; a source that had
        settled an older debt -> that debt re-opens; V twice -> once.
  R2    the six examples of 12.5 (D.amount = 100, settlements from healthy
        money): the obligation goes down by the whole refund, only the
        excess settlement is freed, latest first, and never lands on the
        same debt; a third party's voided lot: a reversal row only, the
        write-off stays. No row is deleted (RV-100); I-CACHE holds.
  quota fully / partially (durable shortfall) / quota changed since the
        reward -> blocked, forced -> not_applicable / resource gone ->
        resource_deleted / before_was_unlimited -> back to 0.
"""
import contextlib
import datetime as dt
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app import models, models_receipt_void as rv
from app.services import receipt_void_effects as void_effects, receipt_void_schema, wallet_accounts
from app.services import wallet_lots, wallet_reconcile, wallet_service, wallet_void

failures = []
GB = 1024 ** 3


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def scenario(label, engine):
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    check(L + "schema ready", receipt_void_schema.bootstrap(engine, Session)["ready"], True)
    with Session() as db:
        db.execute(rv.wallet_runtime_state.update().values(phase="enforced"))
        db.commit()
    counter = iter(range(1, 10_000))

    def operation(db, kind, account):
        epoch = wallet_lots.require_enforced(db)
        return wallet_lots.operation(db, kind, f"{kind}:{next(counter)}", payload={"n": next(counter)},
                                     tenant_scope_key=wallet_lots.tenant_of(db, account), epoch=epoch)[0]

    def customer(name):
        with Session() as db:
            user = wallet_accounts.create_user_with_wallet(db, username=f"{label}-{name}", telegram_id=next(counter) + 50_000)
            db.commit()
            return user.id

    def account(db, user_id):
        return wallet_lots.account_of_user(db, user_id)

    def new_approval(db=None):
        """A minimal 'required' approval row: the causation every source names (FK)."""
        own = db is None
        db = db or Session()
        approval_uuid = str(uuid.uuid4())
        db.execute(rv.receipt_approvals.insert().values(
            approval_uuid=approval_uuid, pending_source_instance_id=f"math-{label}", pending_local_id=next(counter),
            registration_seq=0, immutable_intent_hash="a" * 64, manifest_hash="b" * 64,
            registered_under_mode="required", completeness_generation=1, kind="topup", target_shape="topup",
            approval_mode="manual", original_approval_mode="manual", original_approved_by_telegram_id=1,
            original_approver_evidence_kind="linked_admin", approved_by_telegram_id=1,
            approver_evidence_kind="linked_admin", auto_approve_policy_snapshot="{}", tenant_scope_key="shared",
            telegram_id_snapshot=1, telegram_id_source="user_row", target_user_id_snapshot=1,
            target_username_snapshot="x", amount_snapshot=1, state="completed", created_at=dt.datetime.utcnow(),
            version=0))
        if own:
            db.commit()
            db.close()
        return approval_uuid

    def credit(user_id, amount, kind="receipt_topup", causation=None):
        if kind == "receipt_topup" and causation is None:
            causation = new_approval()                            # a healthy top-up of its own approval
        with Session() as db:
            wallet = account(db, user_id)
            wallet_lots.fence_identity(db, wallet["customer_identity_id"])
            op = operation(db, "wallet_credit_apply", wallet)
            source = wallet_lots.credit(db, wallet, origin_kind=kind, source_key=f"k{next(counter)}", amount=amount,
                                        operation_id=op, causation_approval_uuid=causation)
            db.commit()
            return source

    def sale(user_id, amount):
        with Session() as db:
            wallet = account(db, user_id)
            wallet_lots.fence_identity(db, wallet["customer_identity_id"])
            op = operation(db, "wallet_debit", wallet)
            ref = str(uuid.uuid4())
            debit = wallet_lots.hold(db, wallet, amount=amount, hold_ref=ref, operation_id=op)
            entry = models.LedgerEntry(kind="sale_new", amount=amount, user_id=user_id)
            db.add(entry)
            db.flush()
            wallet_lots.capture(db, ref, entry.id)
            db.commit()
            return debit

    def void(source_id, identity_user_id):
        with Session() as db:
            wallet_lots.require_enforced(db)
            identity = account(db, identity_user_id)["customer_identity_id"]
            op = db.execute(rv.wallet_operations.insert().values(
                operation_type="void_receipt", business_key=str(uuid.uuid4()), request_hash="0" * 64,
                tenant_scope_key="shared", actor_kind="admin", state="running", epoch_at_start=1,
                result_snapshot="{}", attempts=0, created_at=dt.datetime.utcnow(), version=0)).inserted_primary_key[0]
            result = wallet_void.void_source(db, source_id, op, identity)
            db.commit()
            return result

    def refund(debit_id):
        with Session() as db:
            result = wallet_service.refund_debit(db, debit_id)
            db.commit()
            return result["refunded"]

    def state(user_id):
        with Session() as db:
            wallet = account(db, user_id)
            debts = [(int(r.amount), int(r.adjusted_amount), int(r.settled_amount), r.state) for r in db.execute(
                select(rv.wallet_debts).where(rv.wallet_debts.c.wallet_account_id == wallet["id"])
                .order_by(rv.wallet_debts.c.id))]
            balance = int(db.get(models.User, user_id).balance or 0)
            return balance, wallet_lots.open_debt(db, wallet["customer_identity_id"]), debts

    def rows():
        with Session() as db:
            return sum(db.execute(select(func.count()).select_from(table)).scalar_one() for table in (
                rv.wallet_lots, rv.wallet_credit_sources, rv.wallet_debit_events, rv.wallet_allocations,
                rv.wallet_debts, rv.wallet_credit_applications))

    # ================================================================ V (12.6)
    scam = new_approval()
    half = customer("half")
    topup = credit(half, 1000, causation=scam)
    sale(half, 600)
    result = void(topup, half)
    check(L + "V half spent: 400 clawed back, 600 a debt of the identity (reversed_with_debt, value 400)",
          (result.reclaimed, result.debt, result.writeoff, result.reversal_state, result.reversed_value, state(half)),
          (400, 600, 0, "reversed_with_debt", 400, (0, 600, [(600, 0, 0, "open")])))
    check(L + "V twice: reported, nothing voided again", (void(topup, half).already_voided, state(half)[1]), (True, 600))
    clean = customer("clean")
    result = void(credit(clean, 300, causation=scam), clean)
    check(L + "V never spent: fully_reversed, no debt", (result.reversal_state, result.reversed_value, state(clean)),
          ("fully_reversed", 300, (0, 0, [])))
    spent = customer("spent")
    source = credit(spent, 500, causation=scam)
    sale(spent, 500)
    result = void(source, spent)
    check(L + "V fully spent: nothing reclaimed, a 500 debt", (result.reclaimed, result.debt, state(spent)[1]), (0, 500, 500))
    referrer, newcomer = customer("referrer"), customer("newcomer")
    reward = credit(referrer, 200, kind="referral_reward", causation=scam)
    sale(referrer, 150)
    before = rows()
    result = void(reward, newcomer)
    with Session() as db:
        writeoffs = [(int(r.amount), r.reason) for r in db.execute(select(rv.wallet_writeoffs))]
    check(L + "V third party (the referrer spent 150 of a scam's reward): 50 back, 150 a write-off, no debt",
          (result.reclaimed, result.writeoff, result.debt, result.reversal_state, writeoffs, state(referrer)),
          (50, 150, 0, "reversed_with_writeoff", [(150, "third_party_reward_consumed")], (0, 0, [])))
    owes = customer("owes")
    old = credit(owes, 100, causation=new_approval())
    sale(owes, 100)
    void(old, owes)                                              # owes 100
    second_scam = credit(owes, 100, causation=scam)              # settles it entirely
    settled = state(owes)
    result = void(second_scam, owes)
    check(L + "V of a source that had settled an older debt: the settlement reversed, the debt re-opens",
          (settled[1], result.reopened, result.reversed_value, state(owes)), (0, 100, 100, (0, 100, [(100, 0, 0, "open")])))
    check(L + "no wallet row was deleted by V (RV-100)", rows() >= before, True)

    # ================================================================ R2 (12.5): the six examples
    def example(settlements, sales, refund_index):
        user = customer(f"r2-{next(counter)}")
        scam_source = credit(user, 100, causation=new_approval())
        debits = [sale(user, amount) for amount in sales]
        void(scam_source, user)                                   # D.amount = 100
        for amount in settlements:
            credit(user, amount)                                  # healthy money settles D
        refund(debits[refund_index])
        balance, open_debt, debts = state(user)
        return debts[0][1:], balance, open_debt

    #               settled before  sales      refund   -> (adjusted, settled, state), spendable, open
    check(L + "R2 #1: settled 0, r=100 -> closed, nothing new to spend",
          example([], [100], 0), ((100, 0, "settled"), 0, 0))
    check(L + "R2 #2: settled 60, r=100 -> closed, 60 healthy back",
          example([60], [100], 0), ((100, 0, "settled"), 60, 0))
    check(L + "R2 #3: settled 70, r=50 -> closed, 20 healthy freed (partial)",
          example([70], [50, 50], 0), ((50, 50, "settled"), 20, 0))
    check(L + "R2 #4: settled 30, r=50 -> 20 still owed",
          example([30], [50, 50], 0), ((50, 30, "partially_settled"), 0, 20))
    check(L + "R2 #5: settled 100 (30 old + 70 new), r=40 -> 40 freed from the newer settlement",
          example([30, 70], [40, 60], 0), ((40, 60, "settled"), 40, 0))
    check(L + "R2 #6: settled 100, r=100 -> all 100 healthy back, not a rial more",
          example([100], [100], 0), ((100, 0, "settled"), 100, 0))
    with Session() as db:
        latest = db.execute(select(rv.wallet_credit_application_reversals.c.disposition, func.count()).group_by(
            rv.wallet_credit_application_reversals.c.disposition)).all()
    check(L + "...freed settlements are 'reallocate', void reversals 'void_writeoff'",
          sorted(d for d, _n in latest), ["reallocate", "void_writeoff"])
    third = customer("third")
    wallet_reward = credit(third, 100, kind="referral_reward", causation=scam)
    debit = sale(third, 100)
    void(wallet_reward, customer("scammer"))                      # a write-off for the third party
    refund(debit)
    with Session() as db:
        writeoff = db.execute(select(func.count()).select_from(rv.wallet_writeoffs).where(
            rv.wallet_writeoffs.c.wallet_account_id == account(db, third)["id"])).scalar_one()
        reversal = db.execute(select(rv.wallet_allocation_reversals.c.lot_was_voided).order_by(
            rv.wallet_allocation_reversals.c.id.desc())).scalars().first()
    check(L + "R2 over a third party's voided lot: a reversal row only, the write-off stays, nothing spendable",
          (writeoff, reversal, state(third)), (1, True, (0, 0, [])))
    with Session() as db:
        check(L + "I-CACHE holds for every customer", wallet_reconcile.check(db)["problems"], [])

    # ================================================================ quota (14.1)
    with Session() as db:
        owner = wallet_accounts.create_user_with_wallet(db, username=f"{label}-quota", telegram_id=90_001)
        db.flush()
        full = models.Purchase(user_id=owner.id, quota_bytes=15 * GB, used_bytes=2 * GB)
        part = models.Purchase(user_id=owner.id, quota_bytes=15 * GB, used_bytes=12 * GB)
        moved = models.Purchase(user_id=owner.id, quota_bytes=20 * GB, used_bytes=0)
        unlimited = models.Purchase(user_id=owner.id, quota_bytes=5 * GB, used_bytes=1 * GB)
        db.add_all([full, part, moved, unlimited])
        db.flush()
        op = db.execute(rv.wallet_operations.insert().values(
            operation_type="void_receipt", business_key=str(uuid.uuid4()), request_hash="0" * 64,
            tenant_scope_key="shared", actor_kind="admin", state="running", epoch_at_start=1, result_snapshot="{}",
            attempts=0, created_at=dt.datetime.utcnow(), version=0)).inserted_primary_key[0]

        approval_uuid = new_approval(db)
        effect_id = db.execute(rv.receipt_approval_effects.insert().values(
            approval_uuid=approval_uuid, effect_type="quota_reward_granted", effect_key="quota:referral:referrer",
            resource_type="Purchase", resource_id=part.id, actual_projection="{}", resource_snapshot="{}",
            delta_value=5 * GB, created_at=dt.datetime.utcnow())).inserted_primary_key[0]

        def reverse(target, after, **extra):
            return void_effects.reverse_quota(db, target_type="Purchase", target_id=target, granted=5 * GB,
                                              quota_after=after, operation_id=op, user_id_snapshot=owner.id,
                                              before_was_unlimited=extra.pop("unlimited", False),
                                              effect_id=effect_id, **extra)

        check(L + "quota: unused reward fully reversed", (reverse(full.id, 15 * GB), full.quota_bytes),
              (("fully_reversed", 5 * GB), 10 * GB))
        check(L + "quota: 10+5 GB, 12 used -> 3 GB back, a durable 2 GB shortfall, quota = usage",
              (reverse(part.id, 15 * GB), part.quota_bytes, db.execute(select(
                  rv.quota_reversal_shortfalls.c.shortfall_bytes)).scalars().all()),
              (("partially_reversed", 3 * GB), 12 * GB, [2 * GB]))
        try:
            reverse(moved.id, 15 * GB)
            blocked = "not blocked"
        except void_effects.StepBlocked as exc:
            blocked = exc.code
        check(L + "quota changed since the reward: blocked (cleanup_required); forced: not_applicable, untouched",
              (blocked, reverse(moved.id, 15 * GB, forced=True), moved.quota_bytes),
              ("quota_changed_since_reward", ("not_applicable", None), 20 * GB))
        check(L + "quota on a resource already gone: resource_deleted", reverse(999_999, 15 * GB),
              ("resource_deleted", None))
        check(L + "quota granted on an unlimited resource: back to 0 (unlimited), the whole grant",
              (reverse(unlimited.id, 5 * GB, unlimited=True), unlimited.quota_bytes), (("fully_reversed", 5 * GB), 0))
        db.rollback()


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="void_math_"))
    sqlite = create_engine(f"sqlite:///{folder}/t.db", connect_args={"check_same_thread": False})
    cleanup.callback(sqlite.dispose)

    @event.listens_for(sqlite, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    scenario("sqlite", sqlite)
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        maria = scratch.claim(url)
        cleanup.callback(scratch.release, maria)
        scenario("mariadb", maria)
    elif os.environ.get("CI"):
        check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
    else:
        print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI)")

check("no network", _no_network.attempts, [])
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
