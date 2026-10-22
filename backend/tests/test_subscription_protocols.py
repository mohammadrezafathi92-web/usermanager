"""Regression: on the public subscription, a PPTP service and a V2Ray/Xray
service each carry their OWN protocol - kind, content, download name and
place in the app-import link - and stay in their own Purchase."""
import base64
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "subscription-local-test-only")

from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models  # noqa: E402
from app.routers import subscription  # noqa: E402


def no_network(*args, **kwargs):
    raise AssertionError("This test must not contact a node or network")


socket.socket.connect = no_network
socket.create_connection = no_network
Base.metadata.create_all(engine)
checks = 0


def check(label, condition):
    global checks
    assert condition, label
    checks += 1
    print("PASS", label)


with SessionLocal() as db:
    mikrotik = models.Node(name="mt-old", type=models.NodeType.mikrotik, mt_endpoint_host="vpn.invalid", mt_pptp_port=1723)
    xray_node = models.Node(name="x-hiddify", type=models.NodeType.xray, xr_panel_mode="hiddify",
                            xr_public_host="https://sub.invalid/path")
    user = models.User(username="mixed", subscription_token="mixed-token", status=models.UserStatus.active)
    db.add_all([mikrotik, xray_node, user])
    db.flush()
    office = models.Purchase(user_id=user.id, comment="Office", quota_bytes=10, status=models.UserStatus.active)
    phone = models.Purchase(user_id=user.id, comment="Phone", quota_bytes=10, status=models.UserStatus.active)
    db.add_all([office, phone])
    db.flush()
    pptp = models.Connection(user_id=user.id, purchase_id=office.id, node_id=mikrotik.id,
                             type=models.ConnectionType.pptp, ppp_username="office-pptp", ppp_password="pw-1", enabled=True)
    xray = models.Connection(user_id=user.id, purchase_id=phone.id, node_id=xray_node.id,
                             type=models.ConnectionType.xray, xr_uuid="11111111-2222-3333-4444-555555555555",
                             xr_email="phone@mixed", enabled=True)
    db.add_all([pptp, xray])
    db.commit()
    pptp_id, xray_id, office_id, phone_id = pptp.id, xray.id, office.id, phone.id

    info = subscription.get_subscription_info("mixed-token", db=db)
    conns = {c.id: c for c in info.accounts[0].connections}
    p, x = conns[pptp_id], conns[xray_id]
    check("the PPTP service is typed and built as PPTP", (p.type.value, p.kind) == ("pptp", "pptp"))
    check("...its content is the PPTP details, not a V2Ray link", p.link is None and "نوع VPN: PPTP" in p.config_text)
    check("the V2Ray service is typed and built as Xray", (x.type.value, x.kind) == ("xray", "hiddify_sub"))
    check("...its content is its own subscription link, not PPTP details",
          x.config_text is None and x.link.endswith("/11111111-2222-3333-4444-555555555555/"))
    check("each stays in its own Purchase", (p.purchase_id, x.purchase_id) == (office_id, phone_id))

    imported = base64.b64decode(subscription.get_subscription_app_import("mixed-token", db=db).body).decode()
    check("the app-import link carries the V2Ray service", "11111111-2222-3333-4444-555555555555" in imported)
    check("...and never the PPTP one", "office-pptp" not in imported and "PPTP" not in imported)

    download = subscription.download_connection_config("mixed-token", pptp_id, db=db)
    disposition = download.headers["content-disposition"]
    check("the PPTP download is named as PPTP", "-pptp-" in disposition and disposition.endswith('.txt"'))
    check("...and holds the PPTP details", "نوع VPN: PPTP" in download.body.decode())
    xdownload = subscription.download_connection_config("mixed-token", xray_id, db=db)
    check("the V2Ray download is named as Xray and holds its link",
          "-xray-" in xdownload.headers["content-disposition"] and "sub.invalid" in xdownload.body.decode())

print(f"\n{checks} checks passed")
