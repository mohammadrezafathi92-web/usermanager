"""routers/subscription.py's get_subscription_app_import - the plain-text
"Subscribe" URL a VPN app imports (what the bot's "🔗 لینک ساب شما" hands
out, see telegram_bot/handlers/customer_account.py's cb_sublink).

Run:  python3 backend/tests/test_subscription_multi_account.py

Reported 2026-09-23: "این باید لینک سابی رو بده که باهاش همه اکانت ها رو
نشون بده" - a customer with two separate purchases ends up with two
separate User rows sharing one telegram_id (see User.telegram_id's
docstring), each with its own subscription_token. The bot's message
already claimed the link "همه‌ی سرویس‌های V2ray/Xray شما را با هم نشان
می‌دهد" but that promise only held within ONE account - a service bought
under the second username never showed up on the first account's link.
Pins down the fix: either account's token now returns BOTH accounts'
Xray links combined, and an unlinked (no telegram_id) account is
unaffected."""
from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models  # noqa: E402
from app.routers import subscription as subscription_router  # noqa: E402
from app.services import user_ops  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

node = models.Node(
    name="node1", type=models.NodeType.mikrotik,
    xr_panel_mode="ssh", xr_ssh_host="1.2.3.4", xr_inbound_tag="in1",
    xr_public_host="vpn.example.com", xr_public_port=443,
)
db.add(node)
db.commit()
db.refresh(node)

# Same customer, two separate purchases -> two separate User rows sharing
# one telegram_id, exactly the scenario User.telegram_id's docstring
# describes.
same_tg = 555111
acc1 = user_ops.create_user_record(db, "cust_multi_1", telegram_id=same_tg)
acc2 = user_ops.create_user_record(db, "cust_multi_2", telegram_id=same_tg)
# Unrelated customer, never linked to a chat - must stay unaffected.
acc3 = user_ops.create_user_record(db, "cust_solo", telegram_id=None)
db.commit()

db.add(models.Connection(
    user_id=acc1.id, node_id=node.id, type=models.ConnectionType.xray,
    xr_email="acc1@x", xr_uuid="uuid-acc1", enabled=True,
))
db.add(models.Connection(
    user_id=acc2.id, node_id=node.id, type=models.ConnectionType.xray,
    xr_email="acc2@x", xr_uuid="uuid-acc2", enabled=True,
))
db.add(models.Connection(
    user_id=acc3.id, node_id=node.id, type=models.ConnectionType.xray,
    xr_email="acc3@x", xr_uuid="uuid-acc3", enabled=True,
))
db.commit()

token1 = user_ops.ensure_subscription_token(db, acc1)
token2 = user_ops.ensure_subscription_token(db, acc2)
token3 = user_ops.ensure_subscription_token(db, acc3)
db.commit()

def decoded_links(token: str) -> list[str]:
    resp = subscription_router.get_subscription_app_import(token, db)
    body = base64.b64decode(resp.body).decode("utf-8")
    return [l for l in body.splitlines() if l]


links_via_acc1 = decoded_links(token1)
links_via_acc2 = decoded_links(token2)
links_via_acc3 = decoded_links(token3)

check("acc1's link includes acc1's own uuid", any("uuid-acc1" in l for l in links_via_acc1), True)
check("acc1's link ALSO includes acc2's uuid (combined)", any("uuid-acc2" in l for l in links_via_acc1), True)
check("acc2's link, symmetrically, includes acc1's uuid", any("uuid-acc1" in l for l in links_via_acc2), True)
check("acc1's link count == 2", len(links_via_acc1), 2)
check("acc2's link count == 2", len(links_via_acc2), 2)
check("solo (no telegram_id) account is unaffected - only its own link", links_via_acc3, [
    l for l in links_via_acc3 if "uuid-acc3" in l
])
check("solo account link count == 1", len(links_via_acc3), 1)

print()
if failures:
    print(f"{len(failures)} failure(s): {failures}")
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
sys.exit(0)
