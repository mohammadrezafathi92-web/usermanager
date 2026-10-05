"""Receipt Void phase P3 (third batch) - idempotent registration, retry,
manual takeover, approver evidence and the off/shadow answer (design 5.1,
5.6, 6.4).
Run:  python3 backend/tests/test_receipt_approval_registration.py
"""
from __future__ import annotations

import dataclasses
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models, models_receipt_void as rv
from app.services import bot_auth, receipt_void_schema
from app.services import receipt_approval_intent as ri
from app.services import receipt_approval_registration as reg
from app.services import receipt_approval_runtime as runtime

failures: list[str] = []
A, X, F, AU, SH = (rv.receipt_approvals, rv.receipt_approval_expected_effects, rv.receipt_approval_effects,
                   rv.receipt_approval_authority_events, rv.receipt_approval_shadow_events)


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def attempt(principal, intent, mode, approver=None):
    try:
        result = reg.register(db, principal, intent, approval_mode=mode, approved_by_telegram_id=approver)
        db.commit()
        return result
    except reg.RegistrationRejected as exc:
        db.rollback()
        return (exc.status, exc.code)


def row(uuid):
    db.expire_all()
    return dict(db.execute(select(A).where(A.c.approval_uuid == uuid)).mappings().one())


engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
models.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
receipt_void_schema.bootstrap(engine, Session)
db = Session()
root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
admin = models.AdminUser(username="adm", hashed_password="x", is_superadmin=False, telegram_id=2000, balance=10**9)
other = models.AdminUser(username="oth", hashed_password="x", is_superadmin=False, telegram_id=3000)
db.add_all([root, admin, other, models.PanelSettings(id=1)])
db.flush()
package = models.Package(name="P", quota_gb=5, duration_days=30, price=1000)
card = models.PaymentCard(card_number="1", card_holder="h", approval_telegram_id=4000)
db.add_all([package, card])
db.flush()
shared_user = models.User(username="ali", telegram_id=111)
owned_user = models.User(username="sara", telegram_id=222, owner_admin_id=admin.id)
key_a = models.ApiKey(key="ka", label="a", key_type=bot_auth.KeyType.REMOTE_SHARED_BOT)
key_b = models.ApiKey(key="kb", label="b", key_type=bot_auth.KeyType.REMOTE_SHARED_BOT)
db.add_all([shared_user, owned_user, key_a, key_b])
db.commit()

internal = bot_auth.BotPrincipal.internal(None)
remote_a, remote_b = bot_auth.BotPrincipal.from_api_key(key_a), bot_auth.BotPrincipal.from_api_key(key_b)
third_party = bot_auth.BotPrincipal(key_id=key_a.id, key_type=bot_auth.KeyType.GLOBAL_INTEGRATION, owner_admin_id=None,
                                    capabilities=frozenset(), label="x")
base = ri.ApprovalIntent(pending_source_instance_id="inst", pending_local_id=1, kind="renew", target_username="ali",
                         amount=1000, package_id=package.id)
counter = [1]


def fresh(**changes):
    counter[0] += 1
    return dataclasses.replace(base, pending_local_id=counter[0], **changes)


print("--- first registration ---")
first = attempt(internal, base, "auto")
stored = row(first.approval_uuid)
check("201, seq 0, token = version 0", (first.created, first.registration_seq, first.execution_token, first.state),
      (True, 0, 0, "registered"))
check("snapshots are the derived identity",
      (stored["kind"], stored["target_shape"], stored["tenant_scope_key"], stored["telegram_id_snapshot"],
       stored["telegram_id_source"], stored["target_user_id_snapshot"], stored["registered_under_mode"],
       stored["completeness_generation"], stored["auto_granted_at"] is not None, stored["original_approval_mode"]),
      ("renew", "renew_user", "shared", 111, "user_row", shared_user.id, "shadow", 0, True, "auto"))
manifest = reg.manifest_of(db, first.approval_uuid)
check("the manifest is stored and its hash matches the approval",
      ([m["effect_type"] for m in manifest],
       ri.manifest_hash([ri.ManifestRow(m["effect_type"], m["effect_key"], m["requirement"], m["expected"]) for m in manifest])
       == stored["manifest_hash"]), (["ledger_sale", "purchase_renewed"], True))

print("--- same pending again ---")
again = attempt(internal, base, "auto")
check("same intent, same key -> 200 with the same uuid and token",
      (again.created, again.approval_uuid == first.approval_uuid, again.execution_token), (False, True, 0))
check("a different intent for the same pending is refused", attempt(internal, dataclasses.replace(base, amount=5), "auto"),
      (409, "intent_changed"))
check("auto from another key", attempt(remote_a, base, "auto"), (403, "execution_principal_mismatch"))
package.quota_gb = 99
db.commit()
check("a retry returns the STORED manifest, not a rebuilt one, even if the package changed since",
      (attempt(internal, base, "auto").approval_uuid, reg.manifest_of(db, first.approval_uuid) == manifest),
      (first.approval_uuid, True))
package.quota_gb = 5
db.commit()

print("--- controlled retry of a failed approval ---")
db.execute(A.update().where(A.c.approval_uuid == first.approval_uuid).values(state="failed", failed_at=reg._now()))
db.commit()
retried = attempt(internal, base, "auto")
check("failed -> registered in place: same uuid, same seq, NEW token, grant untouched",
      (retried.approval_uuid == first.approval_uuid, retried.registration_seq, retried.execution_token, retried.state,
       row(first.approval_uuid)["auto_granted_at"] == stored["auto_granted_at"], row(first.approval_uuid)["failed_at"]),
      (True, 0, 1, "registered", True, None))
events = [dict(r) for r in db.execute(select(AU).order_by(AU.c.id)).mappings()]
check("one authority event: retry, auto -> auto, version 0 -> 1",
      [(e["reason"], e["from_mode"], e["to_mode"], e["from_state"], e["version_before"], e["version_after"]) for e in events],
      [("retry", "auto", "auto", "failed", 0, 1)])

print("--- manual: approver evidence ---")
check("an unknown Telegram id is not an approver", attempt(internal, fresh(), "manual", 9999), (403, "manual_approver_not_authorized"))
check("no approver at all", attempt(internal, fresh(), "manual", None), (403, "manual_approver_not_authorized"))
check("a non-superadmin cannot approve for the shared pool", attempt(internal, fresh(), "manual", 2000),
      (403, "manual_approver_not_authorized"))
check("a third-party integration key can never register a manual approval",
      attempt(third_party, fresh(), "manual", 1000), (403, "manual_requires_managed_bot"))
m_super = attempt(internal, fresh(), "manual", 1000)
m_owner = attempt(internal, fresh(target_username="sara"), "manual", 2000)
seller = models.AdminUser(username="sel", hashed_password="x", is_superadmin=False, parent_admin_id=admin.id)
db.add(seller)
db.flush()
db.add(models.User(username="sellers_customer", telegram_id=333, owner_admin_id=seller.id))
db.commit()
m_root_for_owned = attempt(internal, fresh(target_username="sellers_customer"), "manual", 2000)
check("a superadmin does not implicitly approve for another admin's private tree",
      attempt(internal, fresh(target_username="sara"), "manual", 1000), (403, "manual_approver_not_authorized"))
m_card = attempt(internal, fresh(payment_card_id=card.id), "manual", 4000)
check("superadmin for the shared pool / the owner / the seller's parent admin / the card's approver",
      [(row(m.approval_uuid)["approver_evidence_kind"], row(m.approval_uuid)["approved_by_admin_id"])
       for m in (m_super, m_owner, m_root_for_owned, m_card)],
      [("linked_admin", root.id), ("owner_seller", admin.id), ("linked_admin", admin.id), ("card_approver", None)])
check("an unrelated admin cannot approve another tenant's receipt",
      attempt(internal, fresh(target_username="sara"), "manual", 3000), (403, "manual_approver_not_authorized"))
print("--- the shared bot's own admin list (found on production, 2026-10-04) ---")
db.add(models.BotSettings(id=1, admin_ids="7777, 8888,notanumber"))
db.commit()
m_config = attempt(internal, fresh(), "manual", 7777)
check("a Telegram id in the shared bot's admin list, with no panel account, may approve through the shared bot",
      (row(m_config.approval_uuid)["approver_evidence_kind"], row(m_config.approval_uuid)["approved_by_admin_id"],
       row(m_config.approval_uuid)["approved_by_telegram_id"]), ("linked_admin", None, 7777))
check("...also for a reseller's customer (that admin handles every request of the shared bot)",
      isinstance(attempt(internal, fresh(target_username="sara"), "manual", 8888), reg.Registration), True)
dedicated = bot_auth.BotPrincipal.internal(admin.id)
check("...but not through a bot bound to one owner, and not an id that is not on the list",
      (attempt(dedicated, fresh(target_username="sara", claimed_owner_admin_id=admin.id), "manual", 7777),
       attempt(internal, fresh(), "manual", 9999)),
      ((403, "manual_approver_not_authorized"), (403, "manual_approver_not_authorized")))
manual_row = row(m_super.approval_uuid)
check("manual rows carry no auto grant; original_* is written once",
      (manual_row["auto_granted_at"], manual_row["original_approval_mode"], manual_row["original_approved_by_telegram_id"]),
      (None, "manual", 1000))

print("--- manual takeover ---")
auto_intent = fresh()
auto_first = attempt(remote_a, auto_intent, "auto")
check("registered by key A: execution bound to A's instance uuid",
      row(auto_first.approval_uuid)["execution_key_instance_uuid"], key_a.key_instance_uuid)
check("manual -> auto is never allowed",
      attempt(internal, dataclasses.replace(base, pending_local_id=row(m_super.approval_uuid)["pending_local_id"]), "auto"),
      (409, "approval_is_manual"))
taken = attempt(internal, auto_intent, "manual", 1000)
after = row(auto_first.approval_uuid)
check("auto -> manual by the in-process bot: same uuid, new token, key cleared, the auto grant stays consumed",
      (taken.approval_uuid == auto_first.approval_uuid, taken.execution_token, after["approval_mode"],
       after["execution_key_instance_uuid"], after["auto_granted_at"] is not None, after["original_approval_mode"],
       after["original_approved_by_telegram_id"]), (True, 1, "manual", None, True, "auto", None))
last_event = dict(db.execute(select(AU).order_by(AU.c.id.desc())).mappings().first())
check("authority event: manual_takeover with both sides recorded",
      (last_event["reason"], last_event["from_mode"], last_event["to_mode"], last_event["from_key_instance_uuid"],
       last_event["to_key_instance_uuid"], last_event["to_approver_telegram_id"], last_event["to_approver_evidence_kind"]),
      ("manual_takeover", "auto", "manual", key_a.key_instance_uuid, None, 1000, "linked_admin"))
check("the old key's token is dead: it can no longer even re-register", attempt(remote_a, auto_intent, "auto"), (409, "approval_is_manual"))
same = attempt(internal, auto_intent, "manual", 1000)
check("same key, same approver again -> 200 unchanged", (same.created, same.execution_token), (False, 1))
key_intent = fresh()
attempt(remote_b, key_intent, "manual", 1000)
key_b.enabled = False
db.commit()
attempt(remote_a, key_intent, "manual", 1000)
check("taking over from a revoked key is recorded as such",
      dict(db.execute(select(AU).order_by(AU.c.id.desc())).mappings().first())["reason"], "revoked_key_takeover")
check("a disabled key cannot register anything", attempt(remote_b, fresh(), "manual", 1000), (403, "key_identity_missing"))
card2 = models.PaymentCard(card_number="2", card_holder="h", owner_admin_id=admin.id, approval_telegram_id=4000)
db.add(card2)
db.commit()
approver_intent = fresh(target_username="sara", payment_card_id=card2.id)
attempt(internal, approver_intent, "manual", 2000)
attempt(internal, approver_intent, "manual", 4000)
check("same key, another approver -> approver_change (the first approver stays in original_*)",
      (dict(db.execute(select(AU).order_by(AU.c.id.desc())).mappings().first())["reason"],
       db.execute(select(A.c.original_approved_by_telegram_id, A.c.approved_by_telegram_id)
                  .where(A.c.pending_local_id == approver_intent.pending_local_id)).first()._tuple()),
      ("approver_change", (2000, 4000)))

print("--- once it has effects or is running ---")
busy_intent = fresh()
busy = attempt(internal, busy_intent, "auto")
db.execute(A.update().where(A.c.approval_uuid == busy.approval_uuid).values(state="mutating", mutating_at=reg._now()))
db.commit()
check("same mode + same key -> 200 with the current state, nothing changed",
      (attempt(internal, busy_intent, "auto").state, row(busy.approval_uuid)["version"]), ("mutating", 0))
check("takeover after execution started is refused", attempt(internal, busy_intent, "manual", 1000), (409, "approval_has_effects"))

print("--- after 'cancelled' ---")
cancel_intent = fresh()
cancelled = attempt(internal, cancel_intent, "auto")
db.execute(A.update().where(A.c.approval_uuid == cancelled.approval_uuid).values(state="cancelled", cancellation_reason="username_taken"))
db.commit()
renewed = attempt(internal, cancel_intent, "manual", 1000)
check("a new row: seq + 1, supersedes the cancelled one, fresh manifest",
      (renewed.created, renewed.registration_seq, row(renewed.approval_uuid)["supersedes_approval_uuid"] == cancelled.approval_uuid,
       len(reg.manifest_of(db, renewed.approval_uuid))), (True, 1, True, 2))
check("...and the intent still may not change", attempt(internal, dataclasses.replace(cancel_intent, amount=7), "manual", 1000),
      (409, "intent_changed"))

print("--- auto policy basics ---")
check("topup is never auto", attempt(internal, fresh(kind="topup", package_id=None), "auto"),
      (403, "auto_approval_policy_denied:kind_not_auto_eligible"))
db.add(models.User(username="notg"))
db.commit()
check("no Telegram identity, no auto", attempt(internal, fresh(target_username="notg"), "auto"),
      (403, "auto_approval_policy_denied:no_telegram_identity"))
count_before = db.execute(select(A.c.id)).all()
check("an unknown user / bad package leave no row behind",
      (attempt(internal, fresh(target_username="ghost"), "auto"), attempt(internal, fresh(package_id=99999), "auto"),
       len(db.execute(select(A.c.id)).all()) == len(count_before)),
      ((404, "target_user_not_found"), (422, "package_not_found"), True))

print("--- begin(): the off / shadow answer ---")
off_intent = fresh()
check("off: nothing registered, proceed the legacy way", reg.begin(db, internal, off_intent, approval_mode="auto"),
      {"mode": "off", "approval_uuid": None, "proceed_legacy": True})
runtime.set_requested_mode(db, registration_mode="shadow")
db.commit()
ok = reg.begin(db, internal, off_intent, approval_mode="auto")
check("shadow, success: uuid + token, proceed_legacy false",
      (ok["mode"], bool(ok["approval_uuid"]), ok["execution_token"], ok["proceed_legacy"], ok["created"]),
      ("shadow", True, 0, False, True))
shadow_before = len(db.execute(select(SH.c.id)).all())
failed = reg.begin(db, internal, fresh(target_username="ghost"), approval_mode="auto")
check("shadow, failure: 200-shaped answer with the code, legacy proceeds, one shadow event",
      (failed, len(db.execute(select(SH.c.id)).all()) - shadow_before),
      ({"mode": "shadow", "approval_uuid": None, "shadow_error": "target_user_not_found", "proceed_legacy": True}, 1))
db.execute(rv.receipt_approval_runtime_state.update().values(key_identity_ready=False, key_identity_failure="duplicate_uuid"))
db.commit()
check("shadow with keys not ready: every registration is a shadow error",
      reg.begin(db, internal, fresh(), approval_mode="auto")["shadow_error"], "key_identity_not_ready")
real = reg.register
reg.register = lambda *a, **k: 1 / 0
try:
    db.execute(rv.receipt_approval_runtime_state.update().values(key_identity_ready=True, key_identity_failure=None))
    db.commit()
    crashed = reg.begin(db, internal, fresh(), approval_mode="auto")
finally:
    reg.register = real
check("an unexpected crash inside registration is also only a shadow error - named by its class, with no values",
      crashed["shadow_error"], "registration_error:ZeroDivisionError")

print("--- SQLite: the write lock is taken up front (production, 2026-10-04) ---")
# What production did (reproduced with the app's own engine settings before
# this fix): the registration opens a savepoint and reads; another
# connection commits a write; the registration's own first write then fails
# at once with "database is locked". With the write lock taken first,
# nobody else can commit in between.
import sqlite3
real_register = reg.register
seen = {}


def register_and_probe(db_, *args, **kwargs):
    seen["in_transaction"] = db_.connection().connection.dbapi_connection.in_transaction
    intruder = sqlite3.connect(engine.url.database, timeout=0.1)
    try:
        intruder.execute("INSERT INTO users (username) VALUES ('intruder')")
        intruder.commit()
        seen["intruder"] = "wrote"
    except sqlite3.OperationalError as exc:
        seen["intruder"] = str(exc)
    finally:
        intruder.close()
    return real_register(db_, *args, **kwargs)


reg.register = register_and_probe
try:
    answer = reg.begin(db, internal, fresh(), approval_mode="manual", approved_by_telegram_id=1000)
finally:
    reg.register = real_register
check("the write transaction is open before anything is read, so no other connection can commit in between",
      (seen, answer.get("shadow_error"), bool(answer["approval_uuid"])),
      ({"in_transaction": True, "intruder": "database is locked"}, None, True))
import sqlalchemy
calls = []


def locked(*a, **k):
    calls.append(1)
    raise sqlalchemy.exc.OperationalError("INSERT", {}, Exception("database is locked"))


reg.register = locked
reg.time.sleep = lambda seconds: None
try:
    busy = reg.begin(db, internal, fresh(), approval_mode="manual", approved_by_telegram_id=1000)
finally:
    reg.register = real_register
check("a busy database is retried, then reported by class - and the bot still proceeds",
      (len(calls), busy["shadow_error"], busy["proceed_legacy"]), (reg.WRITE_ATTEMPTS, "registration_error:Exception", True))
db.execute(rv.receipt_approval_runtime_state.update().values(
    registration_mode="required", loyalty_timing_mode="payment_event", completeness_generation=1, key_identity_ready=False,
    key_identity_failure="x"))
db.commit()
try:
    reg.begin(db, internal, fresh(), approval_mode="auto")
    blocked = None
except reg.RegistrationRejected as exc:
    blocked = (exc.status, exc.code)
check("blocked is a hard 503 - never a legacy fallback", blocked, (503, "receipt_approval_blocked"))

print("--- endpoints ---")
from fastapi import HTTPException, Response
from app.routers import receipt_approvals as router
db.execute(rv.receipt_approval_runtime_state.update().values(
    registration_mode="shadow", loyalty_timing_mode="legacy_activation", completeness_generation=0, key_identity_ready=True,
    key_identity_failure=None))
db.commit()
payload = router.ApprovalIntentIn(pending_source_instance_id="inst", pending_local_id=9001, kind="renew",
                                  target_username="ali", amount=1000, package_id=package.id, telegram_id=111,
                                  approved_by_telegram_id=1000)
response = Response()
body = router.register_auto(payload, response, db=db, principal=internal)
check("POST auto: 201 the first time, 200 the second, same uuid",
      (response.status_code, router.register_auto(payload, response, db=db, principal=internal)["approval_uuid"] == body["approval_uuid"],
       response.status_code), (201, True, 200))
check("the auto endpoint ignores approved_by_telegram_id from the body (mode comes from the endpoint)",
      row(body["approval_uuid"])["approval_mode"], "auto")
check("POST manual on the same pending is a takeover",
      (router.register_manual(payload, response, db=db, principal=internal)["execution_token"], response.status_code,
       row(body["approval_uuid"])["approval_mode"]), (1, 200, "manual"))
print("--- who may call the registration endpoints at all (design 6.4) ---")
from app.services.bot_auth import MANAGED_BOT_CAPABILITIES, RECEIPT_AUTO_APPROVAL_WRITE, RECEIPT_MANUAL_APPROVAL_WRITE
check("the two approval capabilities are not ordinary ones: not in ALL_CAPABILITIES, dropped by the parser, "
      "so they cannot be stored on a key or picked in the key-creation UI",
      (MANAGED_BOT_CAPABILITIES & bot_auth.ALL_CAPABILITIES,
       bot_auth.parse_capabilities(bot_auth.serialize_capabilities(bot_auth.ALL_CAPABILITIES)) & MANAGED_BOT_CAPABILITIES
       if bot_auth.parse_capabilities(bot_auth.serialize_capabilities(bot_auth.ALL_CAPABILITIES)) else frozenset(),
       MANAGED_BOT_CAPABILITIES & frozenset().union(*bot_auth.DEFAULT_CAPABILITIES_BY_KEY_TYPE.values())),
      (frozenset(), frozenset(), frozenset()))
legacy_key = models.ApiKey(key="kl", label="legacy")                                   # key_type NULL = legacy global
global_key = models.ApiKey(key="kg", label="global", key_type=bot_auth.KeyType.GLOBAL_INTEGRATION)
tenant_key = models.ApiKey(key="kt", label="tenant", key_type=bot_auth.KeyType.TENANT_INTEGRATION,
                           owner_admin_id=admin.id, scope_enforced=True,
                           capabilities=bot_auth.serialize_capabilities(bot_auth.ALL_CAPABILITIES))
forged_key = models.ApiKey(key="kf", label="forged", key_type=bot_auth.KeyType.GLOBAL_INTEGRATION,
                           capabilities='["customer_read","receipt_manual_approval_write","receipt_auto_approval_write"]')
key_c = models.ApiKey(key="kc", label="c", key_type=bot_auth.KeyType.REMOTE_SHARED_BOT,
                      capabilities=bot_auth.serialize_capabilities(bot_auth.ALL_CAPABILITIES))   # as an existing deployed key has it
db.add_all([legacy_key, global_key, tenant_key, forged_key, key_c])
db.commit()
principals = {name: bot_auth.BotPrincipal.from_api_key(key) for name, key in (
    ("legacy", legacy_key), ("global", global_key), ("tenant", tenant_key), ("forged", forged_key), ("remote", key_c))}
check("granted by what the caller IS: the in-process bot and a panel-minted remote-bot key have both - even a remote "
      "key created before these capabilities existed; every integration key has neither, even with all ordinary "
      "capabilities, even with the names forged into its stored list",
      {name: sorted(p.capabilities & MANAGED_BOT_CAPABILITIES) for name, p in {**principals, "internal": internal}.items()},
      {"legacy": [], "global": [], "tenant": [], "forged": [],
       "remote": sorted(MANAGED_BOT_CAPABILITIES), "internal": sorted(MANAGED_BOT_CAPABILITIES)})


def endpoint_status(fn, principal_, *args):
    try:
        fn(*args, db=db, principal=principal_)
        return 200
    except HTTPException as exc:
        return exc.status_code


gate_payload = router.ApprovalIntentIn(pending_source_instance_id="inst", pending_local_id=9100, kind="renew",
                                       target_username="ali", amount=1000, package_id=package.id, approved_by_telegram_id=1000)
check("auto, manual and finalize are all 403 for every integration key - before anything is read or written",
      {name: (endpoint_status(router.register_auto, principals[name], gate_payload, Response()),
              endpoint_status(router.register_manual, principals[name], gate_payload, Response()),
              endpoint_status(router.finalize, principals[name], body["approval_uuid"], router.FinalizeIn()))
       for name in ("legacy", "global", "tenant", "forged")},
      {name: (403, 403, 403) for name in ("legacy", "global", "tenant", "forged")})
check("...and nothing was registered for that pending",
      db.execute(select(A.c.id).where(A.c.pending_local_id == 9100)).first(), None)
check("the remote bot's key passes the gate (and registers)",
      (endpoint_status(router.register_auto, principals["remote"], gate_payload, Response()),
       db.execute(select(A.c.registered_by_api_key_id).where(A.c.pending_local_id == 9100)).scalar()), (200, key_c.id))

invalid = bot_auth.BotPrincipal._invalid(1, "bad")
try:
    router.register_auto(payload, response, db=db, principal=invalid)
    status = None
except HTTPException as exc:
    status = exc.status_code
check("an invalid principal is refused before anything else", status, 403)
paths = sorted((r.path, tuple(r.methods)) for r in router.bot_router.routes)
check("bot router: the two registration endpoints and finalize, with no superadmin dependency",
      (paths, router.bot_router.dependencies),
      ([("/api/accounting/receipt-approvals/auto", ("POST",)), ("/api/accounting/receipt-approvals/manual", ("POST",)),
        ("/api/accounting/receipt-approvals/{approval_uuid}/finalize", ("POST",))], []))

db.close()
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
