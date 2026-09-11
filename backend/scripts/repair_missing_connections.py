"""Re-creates services the panel believes are ACTIVE but that no longer
exist on the node - the "⚠ کانکشن فعال ... که کلاینتش پیدا نشد" lines from
scripts/report_orphan_connections.py.

The customer is paying, the panel shows the service as active, and nothing
is actually there on the router/V2Ray panel, so it simply doesn't connect.
The usual cause is the delete-to-disable bug documented in
ThreeXUIClient.set_client_enabled's docstring: on an older/classic 3X-UI
API, disabling a client used to DELETE it. When the customer later renewed,
the panel flipped Connection.enabled back to True and considered the job
done - quota_manager._set_connection_enabled returns immediately when
connection.enabled already equals the target, so the self-heal path in
set_client_enabled (which recreates a missing client when ENABLING) never
ran for them. They have been silently dead ever since.

Restores are faithful, not fresh: the peer/client is recreated from the
credentials already stored on the Connection row - same WireGuard public
key and IP, same Xray uuid/email/flow - so every customer's existing config
file or subscription link keeps working untouched. Nothing needs to be
re-sent to anyone.

Only ever touches connections that are enabled in the panel AND missing on
the node. Connections that are DISABLED and missing are left exactly as
they are: for a raw SSH-managed xray node, "disabled" is implemented as
"removed from config.json" (see XrayClient.set_client_enabled), so absence
is correct there; and on 3X-UI a disabled-and-absent client gets recreated
on its own the next time it is legitimately re-enabled.

Never deletes anything. Orphans in the OTHER direction (present on the node,
no row in the panel) are deliberately not handled here - that is a human
decision, see report_orphan_connections.py.

Usage - inside the backend container, dry-run first:

    docker exec -it -w /app usermanager-backend \\
        python3 /opt/usermanager/backend/scripts/repair_missing_connections.py

then, once the list looks right:

    docker exec -it -w /app usermanager-backend \\
        python3 /opt/usermanager/backend/scripts/repair_missing_connections.py --apply

    # optional: one node at a time
    ... repair_missing_connections.py --node 2 --apply
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app import models  # noqa: E402
    from app.database import SessionLocal  # noqa: E402
    from app.services.mikrotik_client import MikrotikClient, MikrotikError  # noqa: E402
    from app.services.user_ops import wg_speed_queue_name  # noqa: E402
    from app.services.xray_client import XrayError, client_for_node  # noqa: E402
except ModuleNotFoundError as exc:
    print(
        f"\n[!] ماژول «{exc.name}» پیدا نشد.\n\n"
        "این اسکریپت باید داخل کانتینر بک‌اند اجرا بشه، نه روی خود سرور -\n"
        "کتابخونه‌های پایتون پنل فقط داخل کانتینرن.\n\n"
        "دستور درست (از پوشه‌ی نصب پنل):\n\n"
        "    docker exec -it -w /app usermanager-backend \\\n"
        '        python3 "$PWD/backend/scripts/repair_missing_connections.py"\n',
        file=sys.stderr,
    )
    sys.exit(2)


def _label(db, conn) -> str:
    user = db.get(models.User, conn.user_id)
    return f"#{conn.id} user={(user.username if user else '?')!r}"


def _repair_mikrotik(db, node, apply: bool) -> tuple[int, int]:
    conns = (
        db.query(models.Connection)
        .filter(
            models.Connection.node_id == node.id,
            models.Connection.type == models.ConnectionType.wireguard,
            models.Connection.enabled.is_(True),
        )
        .all()
    )
    if not conns:
        print("  کانکشن وایرگارد فعالی روی این نود نیست.")
        return 0, 0

    try:
        with MikrotikClient.for_node(node) as mt:
            on_router = {p.get("comment") for p in mt.list_peers(node.mt_wireguard_interface) if p.get("comment")}
            missing = [c for c in conns if c.wg_peer_name and c.wg_peer_name not in on_router]
            if not missing:
                print(f"  همه‌ی {len(conns)} کانکشن فعال روی روتر موجودن - کاری لازم نیست.")
                return 0, 0

            print(f"  {len(missing)} کانکشن فعال که پیرشون روی روتر نیست:")
            done = failed = 0
            for c in missing:
                # Both are stored on the row at provisioning time; without
                # them there is nothing faithful to restore and a fresh key
                # would silently invalidate the customer's existing config.
                if not (c.wg_public_key and c.wg_client_address):
                    print(f"    - {_label(db, c)}  [رد شد: کلید/آدرس ذخیره‌شده نداره]")
                    failed += 1
                    continue
                print(f"    - {_label(db, c)}  peer={c.wg_peer_name!r}  ip={c.wg_client_address}")
                if not apply:
                    continue
                try:
                    mt.add_peer(
                        node.mt_wireguard_interface,
                        public_key=c.wg_public_key,
                        allowed_address=c.wg_client_address,
                        comment=c.wg_peer_name,
                    )
                    if c.speed_limit_mbps:
                        mt.upsert_simple_queue(
                            wg_speed_queue_name(c.wg_peer_name), c.wg_client_address, c.speed_limit_mbps
                        )
                    done += 1
                except MikrotikError as exc:
                    print(f"      [خطا] {exc}")
                    failed += 1
            return done, failed
    except MikrotikError as exc:
        print(f"  [رد شد - قابل اتصال نیست] {exc}")
        return 0, 0


def _repair_xray(db, node, apply: bool) -> tuple[int, int]:
    conns = (
        db.query(models.Connection)
        .filter(
            models.Connection.node_id == node.id,
            models.Connection.type == models.ConnectionType.xray,
            models.Connection.enabled.is_(True),
        )
        .all()
    )
    if not conns:
        print("  کانکشن V2Ray فعالی روی این نود نیست.")
        return 0, 0

    ssh_managed = getattr(node, "xr_panel_mode", "ssh") != "3xui"
    try:
        with client_for_node(node) as xc:
            on_node = set(xc.list_client_emails(node.xr_inbound_tag))
            missing = [c for c in conns if c.xr_email and c.xr_email not in on_node]
            if not missing:
                print(f"  همه‌ی {len(conns)} کانکشن فعال روی V2Ray موجودن - کاری لازم نیست.")
                return 0, 0

            print(f"  {len(missing)} کانکشن فعال که کلاینتشون روی V2Ray نیست:")
            if ssh_managed and apply:
                # XrayClient.add_client rewrites config.json and restarts the
                # service on EVERY call - dozens of restarts in a row would
                # drop live sessions repeatedly. Worth saying out loud before
                # it happens rather than discovering it from the customers.
                print(
                    f"    [توجه] این نود SSH-managed است: هر کلاینت یعنی یک ری‌استارت xray "
                    f"({len(missing)} بار). اتصال‌های زنده لحظه‌ای قطع می‌شن."
                )
            done = failed = 0
            for c in missing:
                if not c.xr_uuid:
                    print(f"    - {_label(db, c)}  [رد شد: uuid ذخیره‌شده نداره]")
                    failed += 1
                    continue
                print(f"    - {_label(db, c)}  email={c.xr_email!r}")
                if not apply:
                    continue
                try:
                    xc.add_client(node.xr_inbound_tag, c.xr_email, c.xr_uuid, c.xr_flow or "")
                    done += 1
                except XrayError as exc:
                    print(f"      [خطا] {exc}")
                    failed += 1
            return done, failed
    except XrayError as exc:
        print(f"  [رد شد - قابل اتصال نیست] {exc}")
        return 0, 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="واقعاً بساز (بدون این فلگ فقط گزارش می‌ده و چیزی تغییر نمی‌کنه)")
    ap.add_argument("--node", type=int, default=None, help="فقط همین یک نود (شناسه‌ی نود)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        q = db.query(models.Node).order_by(models.Node.id)
        if args.node is not None:
            q = q.filter(models.Node.id == args.node)
        nodes = q.all()
        if not nodes:
            print("نودی پیدا نشد.")
            return 0

        if not args.apply:
            print(">>> حالت آزمایشی (dry-run): هیچ چیزی ساخته نمی‌شه. برای اجرای واقعی --apply بزن.\n")

        total_done = total_failed = 0
        for node in nodes:
            state = "" if node.enabled else "  [غیرفعال]"
            print(f"\n=== نود #{node.id} - {node.name}{state} ({node.type.value}) ===")
            if node.type == models.NodeType.mikrotik:
                done, failed = _repair_mikrotik(db, node, args.apply)
            elif node.type == models.NodeType.xray:
                done, failed = _repair_xray(db, node, args.apply)
            else:
                print(f"  [رد شد - نوع نود پشتیبانی‌نشده: {node.type}]")
                continue
            total_done += done
            total_failed += failed

        print()
        if args.apply:
            print(f"تمام شد: {total_done} سرویس بازسازی شد، {total_failed} مورد ناموفق.")
            print("کانفیگ مشتری‌ها تغییری نکرده - همون کلید/uuid قبلی دوباره ساخته شد.")
        else:
            print("این فقط گزارش بود. برای ساخت واقعی دوباره با --apply اجرا کن.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
