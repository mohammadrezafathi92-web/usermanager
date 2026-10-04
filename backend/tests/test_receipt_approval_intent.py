"""Receipt Void phase P3 (second batch) - target resolution, the immutable
intent hash and the expected-effects manifest (design 5.1, 5.3).
Run:  python3 backend/tests/test_receipt_approval_intent.py
"""
from __future__ import annotations

import dataclasses
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import bot_auth, payment_card_events, payment_cards, receipt_void_schema
from app.services import receipt_approval_intent as ri

failures: list[str] = []
GB = 1024 ** 3


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def rejected(fn, *args):
    try:
        fn(*args)
    except ri.IntentRejected as exc:
        return (exc.status, exc.code)
    except HTTPException as exc:
        return (exc.status_code, "http")
    return None


engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
models.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
receipt_void_schema.bootstrap(engine, Session)
db = Session()

root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
admin = models.AdminUser(username="adm", hashed_password="x", is_superadmin=False, balance=10**9)
db.add_all([root, admin, models.PanelSettings(id=1)])
db.flush()
seller = models.AdminUser(username="sel", hashed_password="x", is_superadmin=False, parent_admin_id=admin.id)
node = models.Node(name="n1", type=models.NodeType.mikrotik)
db.add_all([seller, node])
db.flush()
package = models.Package(name="P", quota_gb=10, duration_days=30, price=100000)
db.add(package)
db.flush()
pc_b = models.PackageConnection(package_id=package.id, node_id=node.id, protocol=models.ConnectionType.wireguard)
pc_a = models.PackageConnection(package_id=package.id, node_id=node.id, protocol=models.ConnectionType.wireguard)
db.add_all([pc_b, pc_a])
shared_user = models.User(username="ali", telegram_id=111)
owned_user = models.User(username="sara", telegram_id=222, owner_admin_id=seller.id)
no_tg_user = models.User(username="notg")
db.add_all([shared_user, owned_user, no_tg_user])
db.flush()
purchase = models.Purchase(user_id=shared_user.id, package_id=package.id)
db.add(purchase)
db.commit()

internal = bot_auth.BotPrincipal.internal(None)
third_party = bot_auth.BotPrincipal(key_id=5, key_type=bot_auth.KeyType.GLOBAL_INTEGRATION, owner_admin_id=None,
                                    capabilities=frozenset(), label="x")
base = ri.ApprovalIntent(pending_source_instance_id="inst", pending_local_id=1, kind="new", target_username="newbie",
                         amount=90000, package_id=package.id, list_price=100000, claimed_telegram_id=999)


def variant(**changes):
    return dataclasses.replace(base, **changes)


def keys(rows):
    return [f"{r.effect_type}:{r.effect_key}" for r in rows]


print("--- validation ---")
check("bad kind / missing package / topup without amount / renew id on a non-renew",
      [rejected(ri.resolve_target, db, internal, variant(**c)) for c in (
          {"kind": "link"}, {"package_id": None}, {"kind": "topup", "amount": 0, "package_id": None},
          {"renew_purchase_id": 3})],
      [(422, "invalid_kind"), (422, "package_required"), (422, "invalid_amount"), (422, "renew_purchase_not_applicable")])

print("--- target shapes and derived identity ---")
t_new = ri.resolve_target(db, internal, base)
check("unknown username + kind new -> new_user, Telegram id attested by the managed bot, tenant 'shared'",
      (t_new.shape, t_new.telegram_id, t_new.telegram_id_source, t_new.tenant_scope_key, t_new.user_id),
      ("new_user", 999, "bot_attested", "shared", None))
check("a third-party integration key may not attest a new user", rejected(ri.resolve_target, db, third_party, base),
      (403, "new_user_requires_managed_bot"))
check("a new user needs a Telegram id", rejected(ri.resolve_target, db, internal, variant(claimed_telegram_id=None)),
      (422, "telegram_id_required"))
t_existing = ri.resolve_target(db, internal, variant(target_username="ali", claimed_telegram_id=111))
check("known username + kind new -> existing_user, identity from the user row",
      (t_existing.shape, t_existing.user_id, t_existing.telegram_id, t_existing.telegram_id_source),
      ("existing_user", shared_user.id, 111, "user_row"))
check("a different claimed Telegram id is a mismatch, never an override",
      rejected(ri.resolve_target, db, internal, variant(target_username="ali", claimed_telegram_id=5)), (422, "identity_mismatch"))
t_owned = ri.resolve_target(db, internal, variant(target_username="sara", claimed_telegram_id=None,
                                                  claimed_owner_admin_id=seller.id))
check("a seller's customer belongs to the tenant of the seller's parent Admin",
      (t_owned.owner_admin_id, t_owned.tenant_scope_key), (seller.id, f"admin:{admin.id}"))
check("claiming another owner for an existing user is a mismatch",
      rejected(ri.resolve_target, db, internal, variant(target_username="ali", claimed_telegram_id=None,
                                                        claimed_owner_admin_id=admin.id)) is not None, True)
check("user without Telegram -> source 'none'",
      ri.resolve_target(db, internal, variant(target_username="notg", claimed_telegram_id=None)).telegram_id_source, "none")
renew = variant(kind="renew", target_username="ali", claimed_telegram_id=None)
check("renew shapes", (ri.resolve_target(db, internal, variant(kind="renew", target_username="ali", claimed_telegram_id=None,
                                                                 renew_purchase_id=purchase.id)).shape,
                       ri.resolve_target(db, internal, renew).shape, ri.resolve_target(db, internal, renew).purchase_count),
      ("renew_purchase", "renew_user", 1))
check("renew of an unknown user / unknown purchase",
      (rejected(ri.resolve_target, db, internal, variant(kind="renew")),
       rejected(ri.resolve_target, db, internal, variant(kind="renew", target_username="ali", claimed_telegram_id=None, renew_purchase_id=987))),
      ((404, "target_user_not_found"), (404, "target_purchase_not_found")))
topup = variant(kind="topup", target_username="ali", package_id=None, amount=50000, claimed_telegram_id=None)
check("topup shape", ri.resolve_target(db, internal, topup).shape, "topup")

print("--- intent hash ---")
h = ri.intent_hash(base, t_new)
check("64 hex characters, stable", (len(h), h == ri.intent_hash(base, t_new)), (64, True))
check("every fixed field changes it",
      [ri.intent_hash(variant(**c), t_new) != h for c in (
          {"amount": 1}, {"payment_card_id": 4}, {"receipt_file_id": "f"}, {"discount_code": "X"}, {"referral_code": "R"},
          {"connections": (ri.ConnectionSpec(node.id, "wireguard"),)})], [True] * 6)
check("what the caller CLAIMED and the pending's own id are not in it (identity is the derived one)",
      (ri.intent_hash(variant(claimed_owner_admin_id=77, pending_local_id=2), t_new) == h,
       ri.intent_hash(base, dataclasses.replace(t_new, telegram_id=1)) != h), (True, True))
check("discount code case/space does not matter", ri.intent_hash(variant(discount_code=" off10 "), t_new),
      ri.intent_hash(variant(discount_code="OFF10"), t_new))

print("--- manifest ---")
m_new = ri.build_manifest(db, base, t_new)
slot_keys = sorted(f"connection_created:conn:package_connection:{c.id}" for c in (pc_a, pc_b))
check("new_user: user, purchase (it has connections), one row per bundled connection, sale",
      keys(m_new), sorted(slot_keys + ["ledger_sale:sale", "purchase_created:purchase", "user_created:user"]))
by_key = {f"{r.effect_type}:{r.effect_key}": r for r in m_new}
check("user_created expected", by_key["user_created:user"].expected,
      {"username": "newbie", "telegram_id": 999, "owner_admin_id": None, "package_id": package.id,
       "quota_bytes": 10 * GB, "days": 30})
check("dependent rows bind to the user this approval creates",
      (by_key["purchase_created:purchase"].expected["user"], by_key["ledger_sale:sale"].expected,
       by_key[slot_keys[0]].expected),
      (ri.USER_REF, {"user": ri.USER_REF, "ledger_kind": "sale_new", "amount": 90000, "payment_card_id": None},
       {"user": ri.USER_REF, "node_id": node.id, "protocol": "wireguard", "flow": ""}))
check("all required", {r.requirement for r in m_new}, {"required"})
explicit = variant(connections=(ri.ConnectionSpec(node.id, "wireguard"), ri.ConnectionSpec(node.id, "wireguard")))
check("explicit request connections become request slots - two identical specs are two slots",
      [k for k in keys(ri.build_manifest(db, explicit, t_new)) if k.startswith("connection")],
      ["connection_created:conn:request_slot:0", "connection_created:conn:request_slot:1"])
empty_pkg = models.Package(name="E", quota_gb=0, duration_days=0, price=1)
db.add(empty_pkg)
db.commit()
check("new_user with no connection: no Purchase row in the manifest; unlimited days is null",
      (keys(ri.build_manifest(db, variant(package_id=empty_pkg.id), t_new)),
       ri.build_manifest(db, variant(package_id=empty_pkg.id), t_new)[1].expected["days"]),
      (["ledger_sale:sale", "user_created:user"], None))
m_existing = ri.build_manifest(db, variant(target_username="ali"), t_existing)
check("existing_user: purchase + connections + sale, bound to the real user",
      (keys(m_existing), m_existing[-1].expected["user"]),
      (sorted(slot_keys + ["ledger_sale:sale", "purchase_created:purchase"]), {"type": "User", "id": shared_user.id}))
m_renew = ri.build_manifest(db, dataclasses.replace(renew, renew_purchase_id=purchase.id),
                            dataclasses.replace(t_existing, shape="renew_purchase"))
check("renew: renewed + sale_renew",
      (keys(m_renew), m_renew[1].expected, m_renew[0].expected["ledger_kind"]),
      (["ledger_sale:sale", "purchase_renewed:renew"],
       {"user_id": shared_user.id, "purchase_id": purchase.id, "package_id": package.id, "add_quota_bytes": 10 * GB,
        "add_days": 30}, "sale_renew"))
check("a renewal that adds nothing is refused",
      rejected(ri.build_manifest, db, dataclasses.replace(renew, package_id=empty_pkg.id),
               dataclasses.replace(t_existing, shape="renew_user")), (422, "empty_renewal"))
m_topup = ri.build_manifest(db, topup, dataclasses.replace(t_existing, shape="topup"))
check("topup: wallet credit + ledger_topup, no sale",
      (keys(m_topup), m_topup[0].expected),
      (["ledger_topup:topup", "wallet_credit_source_created:credit:receipt_topup"],
       {"user_id": shared_user.id, "amount": 50000, "payment_card_id": None}))
check("unknown package", rejected(ri.build_manifest, db, variant(package_id=98765), t_new), (422, "package_not_found"))

print("--- conditional rows ---")
m_owned = ri.build_manifest(db, variant(target_username="sara"), t_owned)
spend = [r for r in m_owned if r.effect_type == "ledger_credit_spend"]
check("a prepaid reseller's sale also expects their credit spend",
      [(r.effect_key, r.requirement, r.expected) for r in spend],
      [("credit_spend", "required", {"admin_id": seller.id, "amount": 100000})])
seller.billing_mode = "usage"
db.commit()
check("...not for a usage-billed reseller, and never for the shared pool",
      ([r for r in ri.build_manifest(db, variant(target_username="sara"), t_owned) if r.effect_type == "ledger_credit_spend"],
       [r for r in m_new if r.effect_type == "ledger_credit_spend"]), ([], []))
card = payment_cards.create_card(db, None, {"card_number": "1234", "card_holder": "H"})
db.commit()
with_card = variant(payment_card_id=card.id)
check("card payment row only when the card's pool is event_logged (a new pool is)",
      [(r.requirement, r.expected) for r in ri.build_manifest(db, with_card, t_new) if r.effect_type == "card_payment_recorded"],
      [("required", {"card_id": card.id, "amount": 90000})])
payment_card_events.revert_to_legacy(db, None)
db.commit()
check("...and absent for a legacy pool or a zero amount",
      ([r for r in ri.build_manifest(db, with_card, t_new) if r.effect_type == "card_payment_recorded"],
       [r for r in ri.build_manifest(db, variant(payment_card_id=card.id, amount=0), t_new)
        if r.effect_type == "card_payment_recorded"]), ([], []))
code = models.DiscountCode(code="OFF10", kind="percent", value=10)
db.add(code)
db.commit()
discount = [r for r in ri.build_manifest(db, variant(discount_code="off10"), t_new) if r.effect_type == "discount_redeemed"]
check("a discount code is an OPTIONAL row keyed by the code id",
      [(r.effect_key, r.requirement, r.expected["code_id"], r.expected["discount_amount"]) for r in discount],
      [(f"discount:{code.id}", "optional", code.id, 10000)])
check("an unknown code adds nothing",
      [r for r in ri.build_manifest(db, variant(discount_code="NOPE"), t_new) if r.effect_type == "discount_redeemed"], [])

print("--- manifest hash and schemas ---")
check("row order does not change the hash; any value does",
      (ri.manifest_hash(m_new) == ri.manifest_hash(list(reversed(m_new))),
       ri.manifest_hash(m_new) != ri.manifest_hash(ri.build_manifest(db, variant(amount=1), t_new))), (True, True))
bad = [("extra key", {"card_id": 1, "amount": 2, "x": 1}), ("missing key", {"card_id": 1}),
       ("wrong type", {"card_id": "1", "amount": 2}), ("bool is not int", {"card_id": True, "amount": 2})]
results = []
for _label, expected in bad:
    try:
        ri.validate_expected("card_payment_recorded", "card_payment", expected)
        results.append("accepted")
    except ValueError:
        results.append("rejected")
check("the validator wants exactly the schema's keys and types", results, ["rejected"] * 4)
check("bindings: only the two shapes",
      [ri.is_binding(v) for v in ({"type": "User", "id": 1}, {"ref": "effect:user_created:user"}, {"type": "User", "id": 0},
                                  {"type": "Node", "id": 1}, {"ref": "effect:x"}, None, {"type": "User", "id": 1, "x": 1})],
      [True, True, False, False, False, False, False])
check("no schema carries a credential-like key",
      [k for _t, _p, schema in ri._SCHEMAS for k in schema
       if any(word in k for word in ("password", "secret", "key", "token", "psk", "uuid"))], [])
import inspect as pyinspect
source = pyinspect.getsource(ri)
check("the module writes nothing", [w for w in ("db.add(", "db.commit(", ".insert()", ".update()", "db.delete(") if w in source], [])

db.close()
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
