"""Read-only report: WireGuard peers (MikroTik) and V2Ray/Xray clients that
still exist on a node but have no matching Connection row left in this
panel's own database anymore - i.e. nothing here still "owns" them.

Written for the 2026-09 bug report: a WireGuard/Xray peer that could not be
removed (or disabled) by the panel used to fail silently - see
services/quota_manager.py's _set_connection_enabled and
services/user_ops.py's deprovision_connection docstrings for the two bugs
that caused it - and every one of those left exactly this kind of orphan
behind. Those two bugs are now fixed, but they don't clean up orphans that
already exist from before the fix; that's what this script is for.

DOES NOT DELETE ANYTHING. It only reports what it finds - every
match/mismatch, per node, printed to the terminal - so you can look them
over and decide what to actually remove yourself, either by hand on the
router/panel or by asking Claude to remove a specific, confirmed list.

Usage - run it INSIDE the backend container, not on the host. The panel's
Python dependencies (sqlalchemy, paramiko, ...) and its DATABASE_URL only
exist in there; the host's bare python3 has none of them. The whole repo
is bind-mounted into the container by docker-compose.yml, so a plain `git
pull` is enough - no image rebuild needed to pick this file up:

    docker exec -it -w /app usermanager-backend \
        python3 "$PWD/backend/scripts/report_orphan_connections.py"

or, spelling the path out (adjust if your install isn't in /opt):

    docker exec -it -w /app usermanager-backend \
        python3 /opt/usermanager/backend/scripts/report_orphan_connections.py

Only ever reads: MikroTik's /interface/wireguard/peers, the 3X-UI panel's
inbound client list (or the SSH-managed xray config.json), and this
panel's own `connections` table. Nothing is written or removed anywhere.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app import models  # noqa: E402
    from app.database import SessionLocal  # noqa: E402
    from app.services.mikrotik_client import MikrotikClient, MikrotikError  # noqa: E402
    from app.services.xray_client import XrayError, client_for_node  # noqa: E402
except ModuleNotFoundError as exc:
    # Almost always "run on the host instead of inside the container" - the
    # panel's dependencies live in the backend image, never on the host, so
    # the very first import fails with a bare traceback that doesn't say
    # why. Say why.
    print(
        f"\n[!] ماژول «{exc.name}» پیدا نشد.\n\n"
        "این اسکریپت باید داخل کانتینر بک‌اند اجرا بشه، نه روی خود سرور -\n"
        "کتابخونه‌های پایتون پنل فقط داخل کانتینرن.\n\n"
        "دستور درست (از پوشه‌ی نصب پنل):\n\n"
        "    docker exec -it -w /app usermanager-backend \\\n"
        '        python3 "$PWD/backend/scripts/report_orphan_connections.py"\n',
        file=sys.stderr,
    )
    sys.exit(2)


# The panel's OWN WireGuard peer on a node, used to reach Telegram (see
# routers/tg_tunnel.py's _PEER_COMMENT and services/wg_tunnel.py). It is
# panel infrastructure, not a customer service, so it has no Connection row
# and never will - reporting it as "orphaned" was a false positive that
# invited deleting the panel's own Telegram connectivity (seen on the very
# first real run of this script, 2026-09).
_PANEL_OWN_PEER_COMMENTS = {"netcip-telegram-tunnel"}


def _check_mikrotik_node(db, node: models.Node) -> None:
    conns = (
        db.query(models.Connection)
        .filter(
            models.Connection.node_id == node.id,
            models.Connection.type == models.ConnectionType.wireguard,
        )
        .all()
    )
    known_peer_names = {c.wg_peer_name for c in conns if c.wg_peer_name}
    try:
        with MikrotikClient.for_node(node) as mt:
            peers = mt.list_peers(node.mt_wireguard_interface)
    except MikrotikError as exc:
        print(f"  [رد شد - قابل اتصال نیست] {exc}")
        return

    on_router = {p.get("comment") for p in peers if p.get("comment")}
    orphans = [
        p for p in peers
        if p.get("comment")
        and p.get("comment") not in known_peer_names
        and p.get("comment") not in _PANEL_OWN_PEER_COMMENTS
    ]
    panel_own = [p for p in peers if p.get("comment") in _PANEL_OWN_PEER_COMMENTS]
    unnamed = [p for p in peers if not p.get("comment")]
    missing = [c for c in conns if c.wg_peer_name and c.wg_peer_name not in on_router]

    print(f"  {len(peers)} پیر وایرگارد روی روتر، {len(known_peer_names)} کانکشن توی دیتابیس پنل برای این نود.")
    if orphans:
        print(f"  {len(orphans)} پیر بی‌صاحب پیدا شد (روی روتر هست، توی پنل نیست):")
        for p in orphans:
            print(
                f"    - comment={p.get('comment')!r}  address={p.get('allowed-address')}  "
                f"public-key={p.get('public-key')}  disabled={p.get('disabled')}"
            )
    else:
        print("  پیر بی‌صاحبی پیدا نشد.")
    if panel_own:
        print(
            f"  ({len(panel_own)} پیر متعلق به خود پنل - تونل تلگرام - که عمداً بی‌صاحب گزارش نشد. "
            f"این‌ها را حذف نکنید.)"
        )
    if unnamed:
        print(
            f"  {len(unnamed)} پیر روی روتر بدون comment هستند - این‌ها اصلاً قابل تطبیق نیستند "
            f"(احتمالاً دستی روی روتر ساخته شدن، نه از طریق پنل)."
        )
    if missing:
        # The opposite direction, and the one a customer actually feels: the
        # panel thinks this service exists, but there is no peer on the
        # router backing it, so it simply does not work.
        print(
            f"  ⚠ {len(missing)} کانکشن توی پنل هست که پیرش روی روتر پیدا نشد "
            f"(یعنی سرویس عملاً کار نمی‌کنه):"
        )
        for c in missing[:20]:
            user = db.get(models.User, c.user_id)
            print(
                f"    - connection #{c.id}  user={(user.username if user else '?')!r}  "
                f"wg_peer_name={c.wg_peer_name!r}  enabled={c.enabled}"
            )
        if len(missing) > 20:
            print(f"    ... و {len(missing) - 20} مورد دیگر")


# Every xray client this panel creates itself is named
# "{username[:12]}{4 hex}@usermanager.local" (see user_ops.provision_xray).
# An orphan that does NOT look like that was never created by NetCip - it
# is either hand-made on the 3X-UI panel or predates//came from elsewhere,
# and deleting it is a decision only a human can make. One that DOES look
# like ours is far more likely to be a genuine leftover from a delete that
# silently failed, which is what this whole report exists to surface.
_PANEL_EMAIL_SUFFIX = "@usermanager.local"


def _check_xray_node(db, node: models.Node) -> None:
    conns = (
        db.query(models.Connection)
        .filter(
            models.Connection.node_id == node.id,
            models.Connection.type == models.ConnectionType.xray,
        )
        .all()
    )
    known_emails = {c.xr_email for c in conns if c.xr_email}
    try:
        with client_for_node(node) as xc:
            emails = xc.list_client_emails(node.xr_inbound_tag)
    except XrayError as exc:
        print(f"  [رد شد - قابل اتصال نیست] {exc}")
        return

    on_node = set(emails)
    orphans = [e for e in emails if e not in known_emails]
    ours = [e for e in orphans if e.endswith(_PANEL_EMAIL_SUFFIX)]
    foreign = [e for e in orphans if not e.endswith(_PANEL_EMAIL_SUFFIX)]
    missing = [c for c in conns if c.xr_email and c.xr_email not in on_node]

    print(f"  {len(emails)} کلاینت روی پنل/کانفیگ V2Ray، {len(known_emails)} کانکشن توی دیتابیس پنل برای این نود.")
    if orphans:
        print(f"  {len(orphans)} کلاینت بی‌صاحب پیدا شد (روی V2Ray هست، توی پنل نیست):")
        if ours:
            print(f"    ساخته‌ی خود پنل ({len(ours)} مورد - احتمالاً بازمانده‌ی حذف ناموفق):")
            for email in ours:
                print(f"      - email={email!r}")
        if foreign:
            print(
                f"    خارج از الگوی نام‌گذاری پنل ({len(foreign)} مورد - این‌ها را پنل نساخته؛\n"
                f"    یا دستی روی 3X-UI ساخته شدن یا ایمپورت بودن. بدون بررسی حذف نکنید):"
            )
            for email in foreign:
                print(f"      - email={email!r}")
    else:
        print("  کلاینت بی‌صاحبی پیدا نشد.")

    if missing:
        # On an SSH-managed xray node this is EXPECTED for any disabled
        # connection: there is no per-client "enable" flag in a raw xray
        # config.json, so disabling one literally removes it (see
        # XrayClient.set_client_enabled). On a 3X-UI node it is not
        # expected - that panel has a real enable flag - so the two are
        # reported differently.
        ssh_managed = getattr(node, "xr_panel_mode", "ssh") != "3xui"
        disabled_missing = [c for c in missing if not c.enabled]
        enabled_missing = [c for c in missing if c.enabled]
        if ssh_managed and disabled_missing:
            print(
                f"  ({len(disabled_missing)} کانکشن غیرفعال که روی کانفیگ نیستن - "
                f"این روی نود SSH طبیعیه: غیرفعال کردن یعنی حذف از config.json.)"
            )
        elif disabled_missing:
            print(
                f"  {len(disabled_missing)} کانکشن غیرفعال توی پنل هست که کلاینتش روی 3X-UI پیدا نشد "
                f"(روی 3X-UI باید فقط غیرفعال می‌شد، نه حذف)."
            )
        if enabled_missing:
            print(
                f"  ⚠ {len(enabled_missing)} کانکشن فعال توی پنل هست که کلاینتش روی V2Ray پیدا نشد "
                f"(یعنی سرویس عملاً کار نمی‌کنه):"
            )
            for c in enabled_missing[:20]:
                user = db.get(models.User, c.user_id)
                print(
                    f"    - connection #{c.id}  user={(user.username if user else '?')!r}  "
                    f"email={c.xr_email!r}"
                )
            if len(enabled_missing) > 20:
                print(f"    ... و {len(enabled_missing) - 20} مورد دیگر")


def main() -> int:
    db = SessionLocal()
    try:
        nodes = db.query(models.Node).order_by(models.Node.id).all()
        if not nodes:
            print("هیچ نودی توی پنل ثبت نشده.")
            return 0

        for node in nodes:
            state = "" if node.enabled else "  [غیرفعال]"
            print(f"\n=== نود #{node.id} - {node.name}{state} ({node.type.value}) ===")
            if node.type == models.NodeType.mikrotik:
                _check_mikrotik_node(db, node)
            elif node.type == models.NodeType.xray:
                _check_xray_node(db, node)
            else:
                print(f"  [رد شد - نوع نود پشتیبانی‌نشده برای این گزارش: {node.type}]")

        print(
            "\nاین اسکریپت فقط گزارش می‌ده و هیچ چیزی حذف نمی‌کنه. مواردی که «بی‌صاحب» گزارش "
            "شدن رو خودت بررسی کن؛ اگر خواستی حذفشون کنی، بگو کدوم‌ها رو."
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
