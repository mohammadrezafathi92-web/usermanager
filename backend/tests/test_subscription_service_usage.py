"""Regression: four purchases must have distinct OpenVPN files and independent public usage."""
import datetime as dt
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "subscription-local-test-only")

from fastapi import HTTPException
from app.database import Base, engine, SessionLocal
from app import models, schemas
from app.routers import subscription
from app.services import user_ops


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
    node = models.Node(name="local-only", type=models.NodeType.mikrotik, mt_endpoint_host="vpn.invalid")
    package = models.Package(name="Three users", quota_gb=30, duration_days=30, max_concurrent_sessions=3)
    user = models.User(username="customer", subscription_token="local-token", total_quota_bytes=999,
                       used_bytes=888, status=models.UserStatus.active)
    stranger = models.User(username="stranger", subscription_token="foreign-token")
    db.add_all([node, package, user, stranger])
    db.flush()
    for name in ("GE-1.ovpn", "GE-2.ovpn"):
        db.add(models.PackageOvpnTemplate(package_id=package.id, name=name,
            content="client\nremote vpn.invalid 1194\nauth-user-pass old.txt\n<auth-user-pass>\nold\nold\n</auth-user-pass>"))
    db.flush()
    purchases, connections = [], []
    gb = 1024 ** 3
    for i, (quota, used) in enumerate(((35, 27), (30, 2), (30, 31), (0, 0))):
        purchase = models.Purchase(user_id=user.id, package_id=package.id, comment=f"Amina{i+1}",
            quota_bytes=quota * gb, used_bytes=used * gb, max_concurrent_sessions=3,
            status=models.UserStatus.active, expire_at=dt.datetime(2030, 1, 1) if quota else None)
        db.add(purchase)
        db.flush()
        connection = models.Connection(user_id=user.id, purchase_id=purchase.id, node_id=node.id,
            type=models.ConnectionType.openvpn, ppp_username=f"service-{i}", ppp_password=f"fake-pass-{i}",
            purchase_batch="same-old-batch", enabled=True, total_bytes=777 * gb)
        db.add(connection)
        purchases.append(purchase)
        connections.append(connection)
    empty = models.Purchase(user_id=user.id, package_id=package.id, comment="No connections",
        quota_bytes=gb, used_bytes=0, status=models.UserStatus.expired)
    db.add(empty)
    db.commit()

    names, contents = [], []
    for connection in connections:
        share = user_ops.get_connection_share(connection)
        check("two templates per service", len(share["ovpn_files"]) == 2)
        for index, profile in enumerate(share["ovpn_files"]):
            expected = f"<auth-user-pass>\n{connection.ppp_username}\n{connection.ppp_password}\n</auth-user-pass>"
            check("only this connection's credentials", profile["content"].count("<auth-user-pass>") == 1
                  and expected in profile["content"] and "old.txt" not in profile["content"])
            response = subscription.download_connection_config("local-token", connection.id, file=index, db=db)
            check("download contains selected connection and template", response.body.decode() == profile["content"])
            check("download and displayed filename agree", response.headers["content-disposition"] == f'attachment; filename="{profile["name"]}"')
            names.append(profile["name"])
            contents.append(profile["content"])
    check("eight stable distinct profile names", len(set(names)) == 8)
    check("four credential pairs produce distinct files", len(set(contents)) == 4)
    check("repeat download preserves profile name", user_ops.get_connection_share(connections[0])["ovpn_files"][0]["name"] == names[0])

    info = subscription.get_subscription_info("local-token", db)
    info = schemas.SubscriptionInfo.model_validate(info).model_dump()
    account = info["accounts"][0]
    by_id = {p["id"]: p for p in account["purchases"]}
    check("all purchases, including no-connection service, are visible", len(by_id) == 5)
    for p in purchases:
        data = by_id[p.id]
        check("usage comes from this Purchase, not user or protocol totals", data["used_bytes"] == p.used_bytes and data["quota_bytes"] == p.quota_bytes)
        check("service cap is independent", data["max_concurrent_sessions"] == 3)
    check("overquota remaining clamps to zero", by_id[purchases[2].id]["remaining_bytes"] == 0)
    check("zero means unlimited and zero usage remains zero", by_id[purchases[3].id]["remaining_bytes"] is None and by_id[purchases[3].id]["used_bytes"] == 0)
    check("public connection links to its own service ID", [c["purchase_id"] for c in account["connections"]] == [p.id for p in purchases])

    try:
        subscription.download_connection_config("foreign-token", connections[0].id, db=db)
    except HTTPException as exc:
        check("unrelated customer's download is still denied", exc.status_code == 404)
    else:
        raise AssertionError("foreign download allowed")

    from app.services.link_builder import ovpn_download_filename
    safe = ovpn_download_filename('../فایل\r\n"evil.ovpn', connections[0], 999)
    check("filename is safe for HTTP headers and paths", safe.isascii() and not any(c in safe for c in '/\\\r\n"'))
    check("name remains tied to connection even for unsafe template names", f"-c{connections[0].id}-t999.ovpn" in safe)

print(f"{checks} checks passed; no network calls")
