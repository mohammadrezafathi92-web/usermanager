"""The panel's and bulk create's package sale lands on THE Purchase its new
connections are on (user_ops.purchase_of_new_connections), on SQLite AND real
MariaDB (CI). Fake remote; no network.

Found 2026-10-18 (with the bot's create_user, fixed in #147): provision_connection
puts every new connection on a Purchase itself, so these paths' absorb returned
None - the panel sale was never bound for refunds (its cap only arrived by
way of the User's), and a sale whose connections were on no single own
Purchase went through silently.

  panel create   the Purchase gets the package cap; on no single own Purchase
                 the sale is refused 409 purchase_target_unresolved and the
                 charge refunded
  bulk create    the Purchase gets the package cap; no single own Purchase is
                 reported as skipped
"""
import contextlib
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("RADIUS_ENABLED", "false")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-panel-bulk-sale-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException

from app import models, models_provisioning as mp, models_reseller_refund as rr, schemas
from app.routers import users as users_router
from app.services import admin_billing, user_ops
import _receipt_void_env as fixture

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def real_persistence(db, user, node, protocol, flow="", *_args, **kwargs):
    connection = models.Connection(user_id=user.id, node_id=node.id, type=protocol, enabled=True,
                                   purchase_batch=kwargs.get("purchase_batch"),
                                   wg_peer_name=f"pb-{user.username}-{node.id}-{getattr(protocol, 'value', protocol)}")
    return user_ops._commit_new_connection(db, user, connection)


def on_no_purchase(db, user, node, protocol, flow="", *_args, **kwargs):
    connection = models.Connection(user_id=user.id, node_id=node.id, type=protocol, enabled=True,
                                   wg_peer_name=f"pn-{user.username}-{node.id}")
    db.add(connection)
    db.commit()
    return connection


def scenario(label, engine):
    L = f"[{label}] "
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack)
        with env.Session() as db:
            db.execute(mp.ProvisioningTypeMode.__table__.update().values(mode="legacy"))
            cap = db.get(models.Package, env.ids.shared).max_concurrent_sessions
            db.commit()

        def reseller_debit(db, admin, package, units=1):
            """A real positive reseller debit, as a volume/credit-billed admin's sale makes."""
            entry = models.LedgerEntry(kind="admin_credit_spend", amount=1000, admin_id=admin.id, package_id=package.id)
            db.add(entry)
            db.commit()
            return entry

        def panel(username, provision, *patches):
            with contextlib.ExitStack() as extra, env.Session() as db:
                extra.enter_context(patch.object(user_ops, "provision_connection", side_effect=provision))
                extra.enter_context(patch.object(admin_billing, "charge_for_package", side_effect=reseller_debit))
                for target, name, value in patches:
                    extra.enter_context(patch.object(target, name, return_value=value))
                refunds = extra.enter_context(patch.object(admin_billing, "refund_for_package",
                                                           wraps=admin_billing.refund_for_package))
                try:
                    users_router.create_user(schemas.UserCreate(username=username, package_id=env.ids.shared),
                                             db=db, admin=env.root, _perm=None, x_idempotency_key=None)
                    answer = "created"
                except HTTPException as exc:
                    answer = exc.detail
                return answer, refunds.call_count

        def purchases(username):
            with env.Session() as db:
                user = db.query(models.User).filter_by(username=username).one()
                return [p.max_concurrent_sessions for p in db.query(models.Purchase).filter_by(user_id=user.id)]

        answer = panel("pbpanel", real_persistence)
        with env.Session() as db:
            user = db.query(models.User).filter_by(username="pbpanel").one()
            purchase = db.query(models.Purchase).filter_by(user_id=user.id).one()
            bound = [b.purchase_id for b in db.query(rr.ResellerSaleBasis).filter_by(purchase_id=purchase.id)]
        check(L + "panel create: the sale is bound for refunds to ITS Purchase, which has the package cap",
              (answer, bound, purchase.max_concurrent_sessions), (("created", 0), [purchase.id], cap))
        check(L + "panel create, connections on no Purchase: refused 409, the charge refunded",
              panel("pbnone", on_no_purchase, (user_ops, "absorb_legacy_pool_into_purchase", None)),
              ("purchase_target_unresolved", 1))

        with patch.object(user_ops, "provision_connection", side_effect=real_persistence), env.Session() as db:
            done = user_ops.bulk_create_users(db, "pbbulk", 1, package_id=env.ids.shared)
        check(L + "bulk create: the Purchase gets the package cap, nothing skipped",
              (done["created"], purchases("pbbulk1"), done["skipped"]), (["pbbulk1"], [cap], []))
        with patch.object(user_ops, "provision_connection", side_effect=on_no_purchase), \
                patch.object(user_ops, "absorb_legacy_pool_into_purchase", return_value=None), env.Session() as db:
            done = user_ops.bulk_create_users(db, "pbbulkx", 1, package_id=env.ids.shared)
        check(L + "bulk create, connections on no Purchase: reported as skipped",
              [s["reason"] for s in done["skipped"]], ["purchase_target_unresolved"])


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="panel_bulk_sale_"))
    sqlite = fixture.sqlite_engine(os.path.join(folder, "t.db"))
    cleanup.callback(sqlite.dispose)
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
