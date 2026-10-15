"""A replay answers only the SAME request (Lifecycle 5.1, 13). Fake runner;
no network.

  * bot purchase / renewal / create_user: the same X-Idempotency-Key with
    another package, price, connections, duration, service or name -> 409,
    nothing new; the identical retry still gets its result;
  * the Mini App's free trial, the same key with another package -> 409;
  * an approval retried with the right token but a changed payload goes
    through the whole approval path and is refused (immutable intent);
  * panel POST /api/users: the retry of a creation that succeeded gets the
    account (not "name taken"); the same key with another request -> 409;
  * bot delete by username: the same key for ANOTHER customer -> 409, that
    customer untouched (no false {ok: true});
  * panel bulk create: the same batch token with another count, prefix or
    notes -> 409; another admin's (tenant's) retry of a half-finished batch
    -> 409, nothing resumed or created for it; the owner then finishes it;
    two requests AT ONCE with the same token and different prefixes: one
    batch, one set of charges, the other request 409.
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-replay-identity-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
os.environ["UM_LOCK_DIR"] = os.path.join(DIRECTORY, "locks")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from fastapi import HTTPException

from app import models, models_provisioning as mp, models_receipt_void as rv, schemas
from app.database import SessionLocal, engine
from app.routers import bot as bot_router, miniapp, users as users_router
from app.services import bot_auth, provisioning_contracts as contracts, provisioning_durable as durable
from app.services import provisioning_parent_execute as execute, provisioning_schema, receipt_void_schema
from app.services import receipt_approval_intent as ri, receipt_approval_registration as registration
from app.services import remote_action as action, user_ops, wallet_accounts
from app.services.provisioning_host import HostIdentity

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


models.Base.metadata.create_all(engine)
check("schemas ready", (receipt_void_schema.bootstrap(engine, SessionLocal)["ready"],
                        provisioning_schema.bootstrap(engine, SessionLocal)["ready"]), (True, True))
HOST = HostIdentity("f6" * 32, str(uuid.uuid4()))
durable.read_identity = lambda: HOST
with SessionLocal() as db:
    host = db.get(mp.ProvisioningRuntimeState, 1)
    host.owner_state, host.owner_host_id, host.owner_boot_id = "active", HOST.host_id, HOST.boot_id
    host.owner_claimed_at = host.owner_heartbeat_at = host.lock_verified_at = dt.datetime.utcnow()
    host.ownership_epoch, host.gate_mode, host.lock_backend = 1, "enforced", "flock"
    db.add(models.PanelSettings(id=1))
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
    reseller = models.AdminUser(username="res", hashed_password="x", balance=100_000)
    rival = models.AdminUser(username="rival", hashed_password="x", balance=100_000)
    db.add_all([root, reseller, rival])
    db.flush()
    node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.23.0.1",
                       mt_wireguard_interface="wg0", mt_client_subnet="10.95.0.0/24")
    db.add(node)
    db.flush()
    db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
        adapter_version=contracts.adapter_version(), config_fingerprint=contracts.config_fingerprint(node),
        server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=root.id,
        verified_at=dt.datetime.utcnow()))
    for admin in (reseller, rival):
        db.add(models.AdminNodeAccess(admin_id=admin.id, node_id=node.id))
    p = models.Package(name="P", quota_gb=5, duration_days=30, price=400, cooperation_price=300,
                       owner_admin_id=reseller.id)
    q = models.Package(name="Q", quota_gb=9, duration_days=60, price=700, cooperation_price=500,
                       owner_admin_id=reseller.id)
    shared = models.Package(name="S", quota_gb=10, duration_days=30, price=1000)
    free_a = models.Package(name="FreeA", quota_gb=1, duration_days=1, price=0)
    free_b = models.Package(name="FreeB", quota_gb=2, duration_days=1, price=0)
    db.add_all([p, q, shared, free_a, free_b])
    db.flush()
    for item in (p, q, shared, free_a, free_b):
        db.add(models.PackageConnection(package_id=item.id, node_id=node.id, protocol="wireguard"))
    wallet_accounts.create_user_with_wallet(db, username="cust", telegram_id=990, owner_admin_id=reseller.id,
                                            balance=50_000)
    for name, tg in (("del1", 991), ("del2", 992)):
        wallet_accounts.create_user_with_wallet(db, username=name, telegram_id=tg, owner_admin_id=reseller.id)
    wallet_accounts.create_user_with_wallet(db, username="approved", telegram_id=993)
    db.commit()
    RESELLER, RIVAL, P, Q, SHARED, FREE_A, FREE_B, NODE = (reseller.id, rival.id, p.id, q.id, shared.id, free_a.id,
                                                           free_b.id, node.id)
    db.execute(rv.receipt_approval_runtime_state.update().values(registration_mode="shadow", key_identity_ready=True,
                                                                 key_identity_failure=None))
    db.commit()
for kind in mp.OPERATION_TYPES:
    with SessionLocal() as db:
        db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.commit()
PRINCIPAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
NO_SHARE = patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node"))


def ok_runner(dto):
    if (dto.params or {}).get("recovery_read"):
        return action.RemoteActionResult(dto.action_id, action.Outcome.ALREADY_PRESENT_VERIFIED, write_attempted=False)
    if dto.action_type is action.ActionType.WG_ENSURE_ABSENT:
        return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                         remote_outcome="verified_absent")
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


RUNNER = patch.object(execute.remote_runner, "run_action", side_effect=ok_runner)


def code(call):
    try:
        return call()
    except HTTPException as exc:
        return exc.status_code, exc.detail if isinstance(exc.detail, str) else exc.detail.get("error")


def counts():
    with SessionLocal() as db:
        return (db.query(mp.ProvisioningOperation).count(), db.query(models.Purchase).count(),
                db.query(models.User).count(), db.get(models.AdminUser, RESELLER).balance)


# ------------------------------------------------------------------ bot purchase
def buy(key, package=P, paid=400, connections=None):
    with SessionLocal() as db, NO_SHARE, RUNNER:
        return code(lambda: [c.id for c in bot_router.purchase_package("cust", schemas.BotPurchasePackageRequest(
            package_id=package, paid_amount=paid, payment_method="card", connections=connections or []),
            db=db, owner_admin_id=None, principal=PRINCIPAL, x_idempotency_key=key).connections])


key = str(uuid.uuid4())
first = buy(key)
before = counts()
check("purchase: the identical retry gets its result", buy(key), first)
check("...the same key with another package: 409", buy(key, package=Q, paid=700), (409, "provisioning_request_changed"))
check("...with another price: 409", buy(key, paid=999), (409, "provisioning_request_changed"))
check("...with other connections: 409", buy(key, connections=[schemas.BotCreateConnectionSpec(
    node_id=NODE, protocol="openvpn")]), (409, "provisioning_request_changed"))
check("...nothing new: operations, services, users, reseller credit", counts(), before)

# ------------------------------------------------------------------ bot renewal
with SessionLocal() as db:
    purchases = [row.id for row in db.query(models.Purchase).order_by(models.Purchase.id)]


def renew(key, purchase_id, days=30, package=P):
    with SessionLocal() as db, NO_SHARE, RUNNER:
        return code(lambda: bot_router.renew_service("cust", purchase_id, schemas.BotRenewRequest(
            add_gb=5, add_days=days, package_id=package, paid_amount=400, payment_method="card"),
            db=db, owner_admin_id=None, principal=PRINCIPAL, x_idempotency_key=key).username)


key = str(uuid.uuid4())
check("renewal done", renew(key, purchases[0]), "cust")
before = counts()
check("renewal: the identical retry is answered", renew(key, purchases[0]), "cust")
check("...the same key with another duration: 409", renew(key, purchases[0], days=60),
      (409, "provisioning_request_changed"))
buy(str(uuid.uuid4()))                     # a second service of the same customer
with SessionLocal() as db:
    second = max(row.id for row in db.query(models.Purchase))
before = counts()
check("...the same key for another service: 409", renew(key, second), (409, "provisioning_request_changed"))
check("...nothing new", counts(), before)

# ------------------------------------------------------------------ bot create_user
def signup(key, username, package=P):
    with SessionLocal() as db, NO_SHARE, RUNNER:
        return code(lambda: bot_router.create_user(schemas.BotCreateUserRequest(
            username=username, telegram_id=7000 + len(username), package_id=package, owner_admin_id=RESELLER,
            connections=[schemas.BotCreateConnectionSpec(node_id=NODE, protocol="wireguard")], paid_amount=400,
            payment_method="card"), db=db, principal=PRINCIPAL, x_idempotency_key=key).username)


key = str(uuid.uuid4())
check("bot create_user done", signup(key, "newone"), "newone")
before = counts()
check("...the identical retry gets the account (not 'name taken')", signup(key, "newone"), "newone")
check("...the same key with another name: 409", signup(key, "newtwo"), (409, "provisioning_request_changed"))
check("...nothing new", counts(), before)

# ------------------------------------------------------------------ Mini App free trial
def trial(key, package):
    with SessionLocal() as db, NO_SHARE, RUNNER, \
            patch.object(miniapp, "_package_or_404", side_effect=lambda d, o, i: d.get(models.Package, i)), \
            patch.object(miniapp, "_notify_owner_of_sale", return_value=None):
        return code(lambda: miniapp.checkout(miniapp.CheckoutRequest(package_id=package), visitor={
            "owner_admin_id": None, "telegram_id": 4242, "user": {}}, db=db, x_idempotency_key=key)["status"])


key = str(uuid.uuid4())
check("Mini App trial done", trial(key, FREE_A), "done")
before = counts()
check("...the identical retry: done again, the same account", (trial(key, FREE_A), counts()), ("done", before))
check("...the same key with another package: 409", trial(key, FREE_B), (409, "provisioning_request_changed"))
check("...nothing new", counts(), before)

# ------------------------------------------------------------------ approval: right token, changed payload
with SessionLocal() as db:
    result = registration.register(db, PRINCIPAL, ri.ApprovalIntent(
        pending_source_instance_id="r", pending_local_id=1, kind="new", target_username="approved", amount=1000,
        package_id=SHARED, list_price=1000), approval_mode="auto")
    db.commit()


def approved(paid):
    with SessionLocal() as db, NO_SHARE, RUNNER:
        return code(lambda: len(bot_router.purchase_package("approved", schemas.BotPurchasePackageRequest(
            package_id=SHARED, connections=[schemas.BotCreateConnectionSpec(node_id=NODE, protocol="wireguard")],
            paid_amount=paid, payment_method="card", approval_uuid=result.approval_uuid,
            execution_token=result.execution_token, list_price=1000), db=db, owner_admin_id=None,
            principal=PRINCIPAL).connections))


check("approval executed", approved(1000), 1)
before = counts()
check("...the same token with a changed payload: refused by the immutable intent, not answered",
      approved(999), (409, "approval_intent_changed"))
check("...the identical retry is answered", approved(1000), 1)
check("...nothing new", counts(), before)

# ------------------------------------------------------------------ panel POST /api/users
def panel(key, username="panel1", package=P, notes=None):
    with SessionLocal() as db, NO_SHARE, RUNNER:
        admin = db.get(models.AdminUser, RESELLER)
        return code(lambda: users_router.create_user(schemas.UserCreate(
            username=username, package_id=package, notes=notes), db=db, admin=admin, _perm=None,
            x_idempotency_key=key).username)


key = str(uuid.uuid4())
check("panel create done", panel(key), "panel1")
before = counts()
check("panel: the retry of the creation that succeeded gets the account, not 'name taken'", panel(key), "panel1")
check("...the same key with another package: 409", panel(key, package=Q), (409, "provisioning_request_changed"))
check("...the same key with other notes: 409", panel(key, notes="x"), (409, "provisioning_request_changed"))
check("...a NEW key for the same name: still 'name taken'", panel(str(uuid.uuid4()))[0], 400)
check("...nothing new", counts(), before)

# ------------------------------------------------------------------ bot delete by username
def delete(key, username):
    with SessionLocal() as db, RUNNER:
        return code(lambda: bot_router.delete_user(username, db=db, owner_admin_id=None, principal=PRINCIPAL,
                                                   x_idempotency_key=key))


key = str(uuid.uuid4())
check("bot delete del1", delete(key, "del1"), {"ok": True})
check("...the same key again for del1: answered", delete(key, "del1"), {"ok": True})
check("...the same key for ANOTHER customer: 409, not a false ok", delete(key, "del2"),
      (409, "provisioning_request_changed"))
with SessionLocal() as db:
    check("...del2 still exists, del1 is gone", (db.query(models.User).filter_by(username="del2").count(),
                                                db.query(models.User).filter_by(username="del1").count()), (1, 0))

# ------------------------------------------------------------------ panel bulk create
hang = {"present": 0, "done": False}


def second_member_hangs(dto):
    if dto.action_type is action.ActionType.WG_ENSURE_PRESENT:
        hang["present"] += 1
    if not hang["done"] and hang["present"] == 2:
        hang["done"] = True
        return action.RemoteActionResult(dto.action_id, action.Outcome.TIMEOUT_UNKNOWN, write_attempted=True,
                                         error_code="timeout")
    return ok_runner(dto)


def bulk(token, admin_id=RESELLER, runner=ok_runner, prefix="bk", count=2, notes=None):
    # The rival sees the same package (as if shared with them): only actor and
    # tenant differ from the batch's owner.
    with SessionLocal() as db, patch.object(execute.remote_runner, "run_action", side_effect=runner), \
            patch.object(durable, "REQUEST_BUDGET_SECONDS", 2), \
            patch.object(users_router, "_get_scoped_package", side_effect=lambda d, a, i: d.get(models.Package, i)):
        admin = db.get(models.AdminUser, admin_id)
        return code(lambda: users_router.bulk_create_users(schemas.BulkCreateUsersRequest(
            prefix=prefix, count=count, package_id=P, notes=notes), db=db, admin=admin, _perm=None,
            x_idempotency_key=token))


def members(token):
    with SessionLocal() as db:
        return sorted((op.business_key.rsplit(":", 1)[1], op.state) for op in db.query(mp.ProvisioningOperation)
                      .filter(mp.ProvisioningOperation.business_key.like(f"bulk:{token}:%")))


token = str(uuid.uuid4())
first = bulk(token, runner=second_member_hangs)
check("half-finished batch: one created, one in progress", (first["created_count"], len(first["in_progress"])), (1, 1))
started, before = members(token), counts()
check("another admin (another tenant) with the same token: 409", bulk(token, admin_id=RIVAL)[0], 409)
check("...nothing resumed, reported or created for them", (members(token), counts()), (started, before))
check("the same token with another count: 409", bulk(token, count=3)[0], 409)
check("...another prefix: 409", bulk(token, prefix="zz")[0], 409)
check("...other notes: 409", bulk(token, notes="n")[0], 409)
check("...nothing new", (members(token), counts()), (started, before))
with SessionLocal() as db:                 # the unfinished member's step becomes due again
    db.query(mp.ProvisioningStep).update({"next_retry_at": None})
    db.query(mp.ProvisioningOperation).update({"next_retry_at": None})
    db.commit()
done = bulk(token)
check("the owner's identical retry finishes exactly that batch", (done["created_count"], done["in_progress"],
      [state for _, state in members(token)]), (2, [], ["completed", "completed"]))

# ------------------------------------------------------------------ bulk: two requests at once, same token
import threading
import time


def slow_runner(dto):
    time.sleep(0.2)                               # widens the window between the two requests
    return ok_runner(dto)


def unpatched_bulk(token, prefix, gate, out):
    gate.wait()
    with SessionLocal() as db:
        admin = db.get(models.AdminUser, RESELLER)
        out[prefix] = code(lambda: users_router.bulk_create_users(schemas.BulkCreateUsersRequest(
            prefix=prefix, count=2, package_id=P), db=db, admin=admin, _perm=None, x_idempotency_key=token))


token, gate, out = str(uuid.uuid4()), threading.Barrier(2), {}
before = counts()
with patch.object(execute.remote_runner, "run_action", side_effect=slow_runner), NO_SHARE:
    threads = [threading.Thread(target=unpatched_bulk, args=(token, prefix, gate, out)) for prefix in ("ca", "cb")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
winners = sorted(prefix for prefix, result in out.items() if isinstance(result, dict))
losers = sorted(result for result in out.values() if not isinstance(result, dict))
with SessionLocal() as db:
    ops = db.query(mp.ProvisioningOperation).filter(mp.ProvisioningOperation.business_key.like(f"bulk:{token}:%")).all()
    names = sorted(name for (name,) in db.query(models.User.username).filter(models.User.username.in_(
        ["ca1", "ca2", "cb1", "cb2"])))
    cost = 300                                   # P's cooperation price, per member
check("two requests at once with the same token, different prefixes: exactly one batch",
      (len(winners), losers, len(ops)), (1, [(409, "provisioning_request_changed")], 2))
check("...only the winner's two customers exist", names, [f"{winners[0]}1", f"{winners[0]}2"] if winners else None)
check("...and one set of charges: two members, two reservations captured", before[3] - counts()[3], 2 * cost)

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
