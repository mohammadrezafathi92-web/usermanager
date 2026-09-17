"""PPTP, added for devices that speak nothing else.

Run:  python3 backend/tests/test_pptp_protocol.py

Adding a protocol to this panel is not one change, it is the same change in
about twenty places - provisioning, deprovisioning, the config the customer
is handed, the quota/enable toggles, the package picker, the bot's
keyboards, the panel's buttons. Miss one and the failure is silent and
late: the connection is created and then never disabled when the quota runs
out, or it is created and then orphaned on the router when the user is
deleted.

So this test is mostly one question asked repeatedly: wherever SSTP is
named - the protocol PPTP is plumbed identically to - is PPTP named too?

The other half is the part that is NOT like SSTP. PPTP's authentication
(MS-CHAPv2) has been broken since 2012, and Apple and Google have both
removed it from their operating systems. It is offered anyway, on request,
for old Windows boxes and routers - but a customer handed these credentials
has no other way to learn that this one cannot keep their traffic private,
so the warning has to travel WITH the credentials rather than live in a
tooltip in the panel they will never see.
"""
from __future__ import annotations

import inspect
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.services import link_builder, mikrotik_client, user_ops
from app.routers import nodes as nodes_router, packages as packages_router, users as users_router

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


ROOT = pathlib.Path(__file__).resolve().parents[2]


print("--- it exists as a protocol ---")
check("the enum has it", models.ConnectionType.pptp.value, "pptp")
check("a node carries a port for it", hasattr(models.Node, "mt_pptp_port"), True)
check("the router can be told to enable it",
      hasattr(mikrotik_client.MikrotikClient, "push_pptp_config"), True)
check("there is a provisioner", callable(user_ops.provision_pptp), True)
check("...and a request schema", hasattr(schemas, "ConnectionCreatePptp"), True)
check("...and an endpoint to add one", callable(users_router.add_pptp_connection), True)
check("...and one to configure the node", callable(nodes_router.push_pptp_config), True)
check("packages may bundle it", "pptp" in packages_router.MIKROTIK_PROTOCOLS, True)


print("\n--- wherever SSTP is named, PPTP is too ---")
# The whole point: SSTP is the protocol PPTP is plumbed identically to, so
# any file that had to learn about SSTP has to learn about PPTP.
SURFACES = [
    "backend/app/services/user_ops.py",
    "backend/app/routers/users.py",
    "backend/app/telegram_bot/keyboards.py",
    "backend/app/telegram_bot/connection_sender.py",
    "backend/app/telegram_bot/utils.py",
    "frontend/src/api/client.js",
    "frontend/src/pages/UserDetail.jsx",
    "frontend/src/pages/Packages.jsx",
]
for rel in SURFACES:
    text = (ROOT / rel).read_text(encoding="utf-8")
    if "sstp" not in text.lower():
        check(f"{rel} mentions sstp at all", True, False)
        continue
    check(f"{rel} knows about pptp", "pptp" in text.lower(), True)

# The PPP family tuples are the ones that bite silently: a type missing from
# them is created fine and then never disabled, never cleaned up.
ppp_sites = inspect.getsource(user_ops)
check("the quota/enable + deprovision tuples include it",
      ppp_sites.count("models.ConnectionType.pptp") >= 3, True)


print("\n--- and the warning travels with the credentials ---")
engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()
node = models.Node(id=1, name="n", type=models.NodeType.mikrotik,
                   mt_host="10.0.0.1", mt_endpoint_host="vpn.example.ir",
                   mt_username="u", mt_password="p")
db.add(node)
db.commit()
conn = models.Connection(user_id=1, node_id=1, type=models.ConnectionType.pptp,
                         ppp_username="cust1", ppp_password="secret")

text = link_builder.build_pptp_info(conn, node)
check("the config names the server", "vpn.example.ir" in text, True)
check("...the default port", "1723" in text, True)
check("...and the credentials", "cust1" in text and "secret" in text, True)
# The part that matters. A customer who is handed this has no other way to
# find out, and is typically the least technical customer on the panel -
# which is exactly why they asked for the protocol that needs no app.
check("it says the encryption is not safe", "امن نیست" in text, True)
check("...and that modern phones cannot use it", "آیفون" in text, True)

# Nothing else gained a warning by accident.
check("SSTP's config is unchanged",
      "امن نیست" in link_builder.build_sstp_info(conn, node), False)

print("\n--- the panel and the bot label it too ---")
from app.telegram_bot import keyboards, connection_sender  # noqa: E402

check("the bot's protocol picker offers it",
      "pptp" in keyboards.protocols_kb("mikrotik").model_dump_json(), True)
check("...with a warning in the label", "⚠️" in keyboards.PROTOCOL_LABELS["pptp"], True)
check("the config sender knows its port", connection_sender.DEFAULT_PORTS["pptp"], 1723)
check("...and treats it as a PPP protocol",
      '"pptp"' in inspect.getsource(connection_sender), True)

panel = (ROOT / "frontend/src/pages/UserDetail.jsx").read_text(encoding="utf-8")
check("the panel's button carries the warning mark", "PPTP ⚠️" in panel, True)
check("...and a tooltip explaining it", 'title={t("userDetail.pptpWarning")}' in panel, True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("PPTP هست، و همه‌جا با هشدارش هست")
