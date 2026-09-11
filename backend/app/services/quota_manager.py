"""Background job: pull traffic counters from every node, add the deltas
onto each connection's owning user (shared quota across all protocols),
and enable/disable connections as quota / expiry dictate."""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import update
from sqlalchemy.orm import Session, selectinload

from .. import models
from ..database import SessionLocal
from .mikrotik_client import MikrotikClient, MikrotikError, parse_ros_duration_seconds
from .user_ops import _maybe_activate_reserved_renewal, _maybe_activate_reserved_purchase_renewal
from .xray_client import XrayError, client_for_node

logger = logging.getLogger("quota_manager")

PPP_TYPES = (models.ConnectionType.openvpn, models.ConnectionType.l2tp, models.ConnectionType.ikev2, models.ConnectionType.sstp)

# A WireGuard peer whose last cryptographic handshake was more recent than
# this counts as "currently connected" for online-status display and the
# cross-protocol concurrent-session count (see radius_server.py). Peers here
# use a 25s persistent-keepalive (see link_builder.py), and WireGuard itself
# re-handshakes at least every 120s of active traffic, so a live connection
# should never go this long between handshakes - this threshold just adds a
# safety margin over one missed poll/keepalive cycle.
WIREGUARD_ONLINE_THRESHOLD_SECONDS = 180


def _atomic_increment(db: Session, model, obj_id: int, column: str, delta: float) -> None:
    """Atomic `column = column + delta` evaluated by the database itself (a
    raw UPDATE's SET expression - portable SQL, works the same on SQLite
    and MySQL/MariaDB), instead of Python computing `old_value +
    delta` from an ORM attribute read and assigning it back. The scheduler's
    poll_all job and radius_server.py's accounting handler each run on
    their own thread with their own independent SessionLocal(), and can
    both be mid-way through processing the SAME user/purchase/admin at once
    (e.g. a user with one WireGuard connection, polled by the scheduler,
    and one OpenVPN connection, accounted live via RADIUS) - two
    concurrent `obj.used_bytes = obj.used_bytes + delta` read-then-writes
    silently drop whichever one commits last (lost update). A DB-level
    atomic increment can't lose either side no matter how the two threads
    interleave. Immediately expires the now-stale in-Python attribute so
    the next read (e.g. quota_manager._enforce_user_limits's exceeded
    check, called right after this in the same request/job) sees the
    fresh committed value instead of a stale cached one."""
    db.execute(update(model).where(model.id == obj_id).values(**{column: getattr(model, column) + delta}))
    db.expire(db.get(model, obj_id), [column])


def _apply_delta(db: Session, connection: models.Connection, rx: int, tx: int):
    """Given fresh cumulative rx/tx from the node, compute the delta since
    last poll (handling counter resets) and add it to the owning
    allotment's usage - the connection's OWN Purchase (see
    models.Purchase's docstring) if it has one, or the owning User's
    combined used_bytes otherwise (the original, still-default behavior for
    every connection not created via the "افزودن پکیج" flow)."""
    prev_total = (connection.last_rx_bytes or 0) + (connection.last_tx_bytes or 0)
    new_total = (rx or 0) + (tx or 0)

    if new_total >= prev_total:
        delta = new_total - prev_total
    else:
        # counters were reset (peer recreated / xray restarted / ppp session
        # reconnected with a fresh dynamic interface)
        delta = new_total

    if delta <= 0:
        connection.last_rx_bytes = rx
        connection.last_tx_bytes = tx
        return

    connection.last_rx_bytes = rx
    connection.last_tx_bytes = tx
    connection.total_bytes = (connection.total_bytes or 0) + delta

    user: models.User = connection.user
    if connection.purchase_id:
        # Atomic - see _atomic_increment's docstring: a Purchase can have
        # more than one bundled connection, so its used_bytes is a shared
        # target too, not just the User-level field below.
        _atomic_increment(db, models.Purchase, connection.purchase_id, "used_bytes", delta)
    else:
        _atomic_increment(db, models.User, user.id, "used_bytes", delta)

    db.add(models.UsageLog(user_id=user.id, connection_id=connection.id, delta_bytes=delta))

    # Usage-based reseller billing (see AdminUser.billing_mode) - for
    # admins in "usage" mode, this single choke point (every protocol's
    # traffic ends up here - WireGuard/Xray polling, and RADIUS accounting
    # for OpenVPN/L2TP/IKEv2) is where their GB volume pool depletes in
    # near-real-time, instead of being charged a flat price per package at
    # creation time (see routers/users.py's _charge_admin_for_package).
    admin = user.owner_admin
    if admin is not None and not admin.is_superadmin and admin.billing_mode == "usage":
        # Atomic - this is the highest-collision target of the three: every
        # one of an admin's users' connections, across both the scheduler
        # thread and the RADIUS thread, funnels delta events into this same
        # AdminUser row.
        _atomic_increment(db, models.AdminUser, admin.id, "volume_balance_gb", -(delta / (1024 ** 3)))


# models.UsageLog gets ONE row per connection per poll cycle that moved any
# bytes - at the default 30s interval that's ~2,880 rows/day for a single
# busy connection, so a few hundred active services write millions of rows
# a month. Nothing ever deleted them, which is what silently filled the
# panel server's disk (the DB file only ever grows). Everything that READS
# this table only looks at the last 24 hours (dashboard.py's usage chart)
# or the last 60 seconds (its live-throughput figure), so anything older is
# pure dead weight - kept for a week anyway as headroom.
USAGE_LOG_KEEP_DAYS = 7


def cleanup_old_usage_logs(keep_days: int = USAGE_LOG_KEEP_DAYS) -> int:
    """Deletes UsageLog rows older than `keep_days`. Called daily from the
    scheduler (see main.py's _start_full_services), same shape as
    radius_server.py's cleanup_old_radius_limit_logs."""
    db = SessionLocal()
    try:
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=keep_days)
        deleted = (
            db.query(models.UsageLog)
            .filter(models.UsageLog.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        if deleted:
            logger.info("cleaned up %d old usage log row(s) (older than %d day(s))", deleted, keep_days)
        return deleted
    except Exception:
        logger.exception("failed to clean up old usage logs")
        db.rollback()
        return 0
    finally:
        db.close()


def _user_status_from_purchases(user: models.User) -> models.UserStatus:
    """The account-level badge for a customer whose services are ALL
    independent Purchases - derived from those services rather than from
    the user's own combined fields.

    Fixes a real confusion (2026-08-10): after services/purchase_migration
    moved everything to per-service quotas, User.expire_at/used_bytes
    stopped being updated (renewals now extend the PURCHASE), so a customer
    who had renewed and had a perfectly valid service still showed
    «منقضی‌شده» on their account forever, because that frozen old expiry
    date is still in the past and always will be. Their service worked -
    _enforce_user_limits never disables purchase-linked connections - but
    the status said otherwise.

    Any active service means the account is active. Otherwise it reports
    whichever reason the services actually ended for; with no services at
    all, the account is simply active (nothing to enforce)."""
    statuses = [p.status for p in user.purchases]
    if not statuses:
        return models.UserStatus.active
    if models.UserStatus.active in statuses:
        return models.UserStatus.active
    if models.UserStatus.quota_exceeded in statuses:
        return models.UserStatus.quota_exceeded
    if models.UserStatus.expired in statuses:
        return models.UserStatus.expired
    return models.UserStatus.active


def _enforce_user_limits(db: Session, user: models.User):
    """Enforces the user's own COMBINED quota/expiry - only ever governs
    connections with no purchase_id (see Connection.purchase_id's
    docstring); connections linked to an independent Purchase are enforced
    separately by _enforce_purchase_limits below, regardless of what this
    user-level check decides."""
    if user.status == models.UserStatus.disabled:
        return  # manually disabled by admin - do not touch

    # No legacy connections left => the user-level pool governs NOTHING, so
    # judging the account by it would just show a stale verdict forever
    # (see _user_status_from_purchases). Mirror the services instead, and
    # touch no connections - each one is already handled per-purchase.
    if not any(c.purchase_id is None for c in user.connections):
        target = _user_status_from_purchases(user)
        if target != user.status:
            user.status = target
        return

    exceeded = user.total_quota_bytes and user.used_bytes >= user.total_quota_bytes
    expired = user.expire_at and user.expire_at < dt.datetime.utcnow()

    if (exceeded or expired) and _maybe_activate_reserved_renewal(db, user):
        # A queued renewal (see User.reserved_quota_bytes's docstring) just
        # kicked in - re-check against the fresh quota/expiry it just set
        # instead of the stale exhausted ones computed above.
        exceeded = user.total_quota_bytes and user.used_bytes >= user.total_quota_bytes
        expired = user.expire_at and user.expire_at < dt.datetime.utcnow()

    # What the LEGACY connections themselves should do, based purely on the
    # user's own combined quota/expiry - unaffected by any separate
    # Purchase this same account might also have.
    if exceeded:
        legacy_target = models.UserStatus.quota_exceeded
    elif expired:
        legacy_target = models.UserStatus.expired
    else:
        legacy_target = models.UserStatus.active

    # The ACCOUNT-LEVEL badge (user.status) shown in the panel and bot,
    # though, must not say «اتمام حجم»/«منقضی‌شده» while a purchased
    # package on the SAME account is still genuinely valid.
    #
    # BUG FIXED 2026-09 (reported: "account still has one valid package but
    # shows اتمام حجم"): _user_status_from_purchases already handles this
    # correctly for an account made ENTIRELY of Purchases (see its
    # docstring and the early-return branch above), but a mixed account -
    # one still carrying an old legacy connection (pre-migration, or added
    # outside the package flow) ALONGSIDE a newer Purchase - fell through
    # to this legacy-only branch instead, which used to set user.status
    # from the frozen legacy fields alone and never even looked at
    # user.purchases. A stale/exhausted legacy quota then overrode a
    # perfectly valid purchase in what the customer and admin were shown,
    # even though the purchase's own connections kept working fine
    # (_enforce_purchase_limits governs those independently and correctly).
    display_target = legacy_target
    if display_target != models.UserStatus.active and user.purchases:
        if _user_status_from_purchases(user) == models.UserStatus.active:
            display_target = models.UserStatus.active

    if display_target != user.status:
        user.status = display_target

    # Deliberately NOT gated behind "did the badge change" - a purchase
    # masking the badge (display_target == user.status == active, unchanged)
    # must never also mask a legacy connection that just went newly
    # exhausted; that connection still needs disabling even though nothing
    # about user.status moved. _set_connection_enabled already no-ops
    # (skips the MikroTik/Xray call entirely) for any connection whose
    # enabled flag already matches, so running this every time costs
    # nothing extra for the common case - see its own early `if
    # connection.enabled == enabled: return`.
    for conn in user.connections:
        if conn.purchase_id:
            continue  # governed independently by _enforce_purchase_limits
        # Deliberately legacy_target here, NOT display_target - a purchase
        # being valid must never re-enable (or keep enabled) a legacy
        # connection whose OWN quota/expiry is genuinely exhausted; it only
        # changes what the account-level badge says.
        _apply_enabled_state(db, conn, legacy_target == models.UserStatus.active)


def _purchase_ids_needing_enforcement(db: Session, now: dt.datetime) -> list[int]:
    """Which purchases _enforce_purchase_limits could actually change.

    On a 20,000-customer panel that function was called 20,000 times a
    cycle and returned without doing anything roughly 20,000 times: a
    service inside its quota and its date needs no attention at all. The
    expensive part was never the check - it was materialising every
    Purchase and its Connections as ORM objects in order to run it.

    So the same predicate is evaluated first over plain tuples (a few
    columns, no ORM, no relationship loading), and only the rows that come
    out of it are loaded properly. Same logic, same order, same outcome -
    see backend/tests/test_poll_equivalence.py, which runs the old and the
    new selection over the same database and compares every single row.

    Kept deliberately as a mirror of _enforce_purchase_limits below rather
    than as clever SQL: if that function's rules change, this one has to
    change with it, and a reader has to be able to see that at a glance.
    """
    rows = db.query(
        models.Purchase.id,
        models.Purchase.status,
        models.Purchase.quota_bytes,
        models.Purchase.used_bytes,
        models.Purchase.expire_at,
        models.Purchase.reserved_quota_bytes,
        models.Purchase.reserved_duration_days,
        models.Purchase.reserved_package_id,
    ).all()

    out = []
    for (pid, status, quota, used, expire_at,
         res_quota, res_days, res_pkg) in rows:
        if status == models.UserStatus.disabled:
            continue  # manually disabled - _enforce_purchase_limits returns immediately
        exceeded = bool(quota) and (used or 0) >= quota
        expired = expire_at is not None and expire_at < now
        # A queued renewal waiting behind an exhausted service must still be
        # activated even though the status ALREADY says exhausted - so this
        # is not covered by the status-mismatch test below.
        if (exceeded or expired) and (res_quota or res_days or res_pkg):
            out.append(pid)
            continue
        if exceeded:
            target = models.UserStatus.quota_exceeded
        elif expired:
            target = models.UserStatus.expired
        else:
            target = models.UserStatus.active
        if target != status:
            out.append(pid)
    return out


def _user_ids_needing_enforcement(db: Session, now: dt.datetime) -> list[int]:
    """The same idea for _enforce_user_limits.

    Two shapes of customer live in here and they are judged differently:

    - one with LEGACY connections (no purchase_id) is still governed by
      their own combined quota/expiry, so the full function has to run;
      there are very few of these left and they are simply all included.

    - everyone else takes the "derive the account badge from the services"
      branch, which changes nothing unless the derived badge differs from
      the stored one. That is decided here from purchase statuses read as
      tuples, so 20,000 accounts cost one query instead of 20,000 objects.
    """
    legacy_user_ids = {
        uid for (uid,) in db.query(models.Connection.user_id)
        .filter(models.Connection.purchase_id.is_(None))
        .distinct()
        .all()
    }

    # user_id -> list of its purchases' statuses, matching what
    # _user_status_from_purchases walks.
    per_user: dict[int, list] = {}
    for uid, status in db.query(models.Purchase.user_id, models.Purchase.status).all():
        per_user.setdefault(uid, []).append(status)

    out = []
    for uid, status in db.query(models.User.id, models.User.status).all():
        if status == models.UserStatus.disabled:
            continue  # manually disabled - the function returns immediately
        if uid in legacy_user_ids:
            out.append(uid)  # full quota/expiry logic still applies
            continue
        statuses = per_user.get(uid, [])
        if not statuses or models.UserStatus.active in statuses:
            target = models.UserStatus.active
        elif models.UserStatus.quota_exceeded in statuses:
            target = models.UserStatus.quota_exceeded
        elif models.UserStatus.expired in statuses:
            target = models.UserStatus.expired
        else:
            target = models.UserStatus.active
        if target != status:
            out.append(uid)
    return out


def _enforce_purchase_limits(db: Session, purchase: models.Purchase):
    """Per-Purchase counterpart to _enforce_user_limits above - the same
    exceeded/expired/reserved-renewal logic, but scoped to just ONE
    independent package purchase (see models.Purchase's docstring) instead
    of the user's combined fields. Only ever touches connections with
    connection.purchase_id == purchase.id."""
    if purchase.status == models.UserStatus.disabled:
        return  # manually disabled - do not touch

    exceeded = purchase.quota_bytes and purchase.used_bytes >= purchase.quota_bytes
    expired = purchase.expire_at and purchase.expire_at < dt.datetime.utcnow()

    if (exceeded or expired) and _maybe_activate_reserved_purchase_renewal(db, purchase):
        exceeded = purchase.quota_bytes and purchase.used_bytes >= purchase.quota_bytes
        expired = purchase.expire_at and purchase.expire_at < dt.datetime.utcnow()

    if exceeded:
        target_status = models.UserStatus.quota_exceeded
    elif expired:
        target_status = models.UserStatus.expired
    else:
        target_status = models.UserStatus.active

    if target_status == purchase.status:
        return

    purchase.status = target_status
    for conn in purchase.connections:
        _apply_enabled_state(db, conn, target_status == models.UserStatus.active)


def _apply_enabled_state(db: Session, connection: models.Connection, target_active: bool) -> None:
    """Wraps _set_connection_enabled with the concurrent-session override:
    a connection enforce_concurrent_session_limits force-disabled for being
    the excess one over User.max_concurrent_sessions must stay disabled
    even once quota/expiry ALONE would allow it back on - only that
    function's own re-check, once the session count is genuinely back
    within the user's cap, is allowed to clear connection.session_limited
    and re-enable it. Without this, quota enforcement (which re-evaluates
    every poll cycle regardless of session state) would immediately undo
    the kick the very next time it saw target_status == active, flapping
    the peer on the router on and off every single cycle."""
    if target_active and connection.session_limited:
        return
    _set_connection_enabled(db, connection, enabled=target_active)


def active_session_count(db: Session, user_id: int) -> int:
    """How many of this user's connections are active RIGHT NOW, combined
    across every protocol they might have.

    PPP (openvpn/l2tp/ikev2/sstp) is counted from the live
    RadiusActiveSession table - real-time, updated on every Access-Request/
    Accounting packet (see radius_server.py). Everything else (wireguard/
    xray) has no login event the panel is asked to approve, so it's counted
    from Connection.online instead, only ever as fresh as the last poll
    cycle (poll_mikrotik_node / poll_xray_node above).

    Shared by radius_server.py's live accept/reject decision on a new PPP
    login and enforce_concurrent_session_limits below (the reactive
    WireGuard/Xray kick), so the two enforcement points can never disagree
    about what "how many sessions does this user have right now" means."""
    ppp_count = (
        db.query(models.RadiusActiveSession)
        .join(models.Connection, models.Connection.id == models.RadiusActiveSession.connection_id)
        .filter(
            models.Connection.user_id == user_id,
            models.Connection.type.in_(PPP_TYPES),
        )
        .count()
    )
    other_online_count = (
        db.query(models.Connection)
        .filter(
            models.Connection.user_id == user_id,
            models.Connection.type.notin_(PPP_TYPES),
            models.Connection.online.is_(True),
        )
        .count()
    )
    return ppp_count + other_online_count


def enforce_concurrent_session_limits(db: Session) -> int:
    """Reactively kicks a WireGuard/Xray connection that pushed a user over
    their own User.max_concurrent_sessions cap, and releases one back once
    there is genuinely room again.

    OpenVPN/L2TP/IKEv2/SSTP (PPP) sessions are gated LIVE at RADIUS
    Access-Request time (see radius_server.py's use of active_session_count) -
    a login that would exceed the cap is simply refused, so PPP alone never
    needs this. WireGuard and Xray have no such login event: a peer/client
    that already exists on the node just works the moment its own keys/
    credentials are used, with no way for this panel to say no in real
    time. So a customer with, say, one OpenVPN service already connected
    could still open a second, unrelated WireGuard connection with nothing
    to stop them at the moment they do it (reported 2026-09-06) - only this
    periodic check, called from poll_all, can notice and cut the extra one
    back off, at most POLL_INTERVAL_SECONDS after the fact.

    Deliberately narrow: only ever touches non-PPP connections (the ones
    that could not have been prevented live), never kicks more than the
    exact number needed, and never releases one back just because kicking
    it made the count look fine - see the `effective` count below, which
    excludes this function's OWN currently-kicked connections entirely, so
    releasing one is only ever a reaction to some OTHER session of the
    user's having genuinely ended, never to its own side effect. Without
    that distinction this would flap a connection on and off forever: kick
    it -> its own online flag eventually goes False because it's disabled
    -> naive "count looks fine now" logic releases it -> it reconnects,
    online again -> kicked again -> repeat.

    Returns how many connections were toggled (kicked or released), purely
    for poll_all's log line."""
    candidate_user_ids = {
        uid for (uid,) in db.query(models.User.id)
        .filter(models.User.max_concurrent_sessions.isnot(None), models.User.max_concurrent_sessions > 0)
        .all()
    } | {
        uid for (uid,) in db.query(models.Connection.user_id)
        .filter(models.Connection.session_limited.is_(True))
        .distinct()
        .all()
    }
    if not candidate_user_ids:
        return 0

    users = (
        db.query(models.User)
        .options(selectinload(models.User.connections).selectinload(models.Connection.purchase))
        .filter(models.User.id.in_(candidate_user_ids))
        .all()
    )

    toggled = 0
    for user in users:
        limit = user.max_concurrent_sessions or 0
        non_ppp = [c for c in user.connections if c.type not in PPP_TYPES]
        ppp_ids = [c.id for c in user.connections if c.type in PPP_TYPES]
        ppp_count = (
            db.query(models.RadiusActiveSession)
            .filter(models.RadiusActiveSession.connection_id.in_(ppp_ids))
            .count()
            if ppp_ids else 0
        )
        # Excludes this function's OWN already-kicked connections - see the
        # docstring above for why that exclusion is what keeps this from
        # flapping.
        not_limited_online = [c for c in non_ppp if c.online and not c.session_limited]
        effective_count = ppp_count + len(not_limited_online)

        if limit:
            kickable = sorted(
                (c for c in not_limited_online if c.enabled),
                key=lambda c: c.id, reverse=True,  # newest connection first
            )
            excess = effective_count - limit
            for conn in kickable:
                if excess <= 0:
                    break
                conn.session_limited = True
                _set_connection_enabled(db, conn, enabled=False)
                toggled += 1
                excess -= 1

        session_limited_conns = [c for c in non_ppp if c.session_limited]
        if session_limited_conns:
            room = len(session_limited_conns) if not limit else max(limit - effective_count, 0)
            for conn in sorted(session_limited_conns, key=lambda c: c.id):
                if room <= 0:
                    break
                governing_status = (
                    conn.purchase.status if conn.purchase_id and conn.purchase else user.status
                )
                if governing_status != models.UserStatus.active:
                    continue  # quota/expiry still says no - leave it kicked, doesn't consume room
                conn.session_limited = False
                _set_connection_enabled(db, conn, enabled=True)
                toggled += 1
                room -= 1
    return toggled


def _set_connection_enabled(db: Session, connection: models.Connection, enabled: bool):
    """Pushes enabled/disabled to the actual node, and only records it in
    `connection.enabled` if that push is known to have actually happened.

    BUG FIXED 2026-09 (reported: WireGuard/V2Ray connections kept passing
    traffic past their quota, and stayed connected even after the account
    showed «اتمام حجم»): this used to set `connection.enabled = enabled`
    unconditionally after the try block, regardless of whether the remote
    call actually found and changed anything. Two ways that went wrong -

    - wireguard: the peer is looked up by matching RouterOS's `comment`
      field against `connection.wg_peer_name`. If that lookup found no
      match (stale name after a manual router edit, a peer recreated with
      a different comment, an import-time mismatch, etc.) the code used to
      just skip `set_peer_disabled` silently - no error, no log - then
      still mark the connection disabled in the DB one line later. The
      panel and bot both then reported the service as cut off while the
      real peer on the router was never touched and kept working exactly
      as before.

    - xray (3X-UI): ThreeXUIClient.set_client_enabled has its own internal
      fallback chain and, when disabling, used to return normally even if
      every attempt failed and there was nothing left to fall back to (see
      its docstring) - again reported as "done" here with nothing actually
      changed on the panel.

    Now each branch reports back whether it actually applied the change;
    `connection.enabled` (and therefore whether _enforce_purchase_limits/
    _enforce_user_limits will ever try again - see their "only acts on a
    status TRANSITION" docstrings) only moves when the node confirms it.
    Anything that didn't apply is picked up again by
    _reconcile_connection_enabled_state on the next poll cycle instead of
    being silently forgotten forever."""
    if connection.enabled == enabled:
        return
    node: models.Node = connection.node
    applied = True  # PPP has nothing to push to the node - the DB flag alone is the switch
    try:
        if connection.type == models.ConnectionType.wireguard:
            with MikrotikClient.for_node(node) as mt:
                peers = mt.list_peers(node.mt_wireguard_interface)
                match = next((p for p in peers if p.get("comment") == connection.wg_peer_name), None)
                if match:
                    mt.set_peer_disabled(match[".id"], disabled=not enabled)
                else:
                    applied = False
                    logger.warning(
                        "wireguard peer not found on node %s for connection %s (wg_peer_name=%r) - "
                        "could not %s it; will retry next poll cycle",
                        node.id, connection.id, connection.wg_peer_name,
                        "enable" if enabled else "disable",
                    )
        elif connection.type in PPP_TYPES:
            # Authenticated via RADIUS - the RADIUS auth handler checks
            # connection.enabled live on every Access-Request, so flipping
            # the DB flag is all that's needed to cut the user off (RouterOS
            # will simply reject the next re-auth / already-open PPP session
            # will be kicked on the router's own timeout or can be dropped
            # manually if instant cutoff is required).
            pass
        elif connection.type == models.ConnectionType.xray:
            with client_for_node(node) as xc:
                applied = xc.set_client_enabled(
                    node.xr_inbound_tag, connection.xr_email, connection.xr_uuid,
                    connection.xr_flow or "", enabled,
                )
                if not applied:
                    logger.warning(
                        "xray client could not be %s on node %s for connection %s (xr_email=%r); "
                        "will retry next poll cycle",
                        "enabled" if enabled else "disabled", node.id, connection.id, connection.xr_email,
                    )
        if applied:
            connection.enabled = enabled
    except (MikrotikError, XrayError) as exc:
        logger.warning("failed to toggle connection %s: %s", connection.id, exc)


def _reconcile_connection_enabled_state(db: Session, now: dt.datetime) -> int:
    """Every poll cycle, re-checks every WireGuard/Xray connection's
    `enabled` flag against what its governing quota/expiry actually says
    right now, and retries the toggle for any that disagree.

    _enforce_purchase_limits/_enforce_user_limits only ever call
    _set_connection_enabled ONCE, at the moment purchase.status/user.status
    itself transitions (see their own docstrings) - they never revisit a
    connection whose status hasn't changed since last cycle. Combined with
    _set_connection_enabled's old silent-success bug (fixed 2026-09, see
    its docstring), a toggle that failed right at that one moment - node
    briefly unreachable, WireGuard peer not found by its stored comment,
    an Xray panel update rejected - was never attempted again: the status
    said "quota_exceeded" forever after, but the connection kept passing
    traffic forever after too. This is what actually notices and corrects
    that drift (both directions - stuck-on AND, less commonly, stuck-off
    after a renewal), independent of whether a status transition happened
    this cycle. session_limited connections are left alone here - see
    _apply_enabled_state's docstring for why those must never be flipped
    by anything except enforce_concurrent_session_limits' own re-check.

    PPP (openvpn/l2tp/ikev2/sstp) needs none of this - it is gated live at
    RADIUS Access-Request time, never has a node-side "enabled" push that
    can fail out of sync with the DB.

    Deliberately two lean queries (purchase-linked, then legacy) rather
    than loading every connection as a full ORM graph - same reasoning as
    poll_all's own _purchase_ids_needing_enforcement/
    _user_ids_needing_enforcement helpers just above it."""
    toggled = 0

    purchase_rows = (
        db.query(models.Connection.id, models.Purchase.quota_bytes, models.Purchase.used_bytes,
                  models.Purchase.expire_at, models.Connection.enabled)
        .join(models.Purchase, models.Connection.purchase_id == models.Purchase.id)
        .filter(models.Connection.type.in_((models.ConnectionType.wireguard, models.ConnectionType.xray)))
        .filter(models.Connection.session_limited.is_(False))
        .filter(models.Purchase.status != models.UserStatus.disabled)
        .all()
    )
    mismatched_ids = []
    for conn_id, quota, used, expire_at, enabled in purchase_rows:
        exceeded = bool(quota) and (used or 0) >= quota
        expired = expire_at is not None and expire_at < now
        should_be_active = not (exceeded or expired)
        if enabled != should_be_active:
            mismatched_ids.append((conn_id, should_be_active))

    legacy_rows = (
        db.query(models.Connection.id, models.User.total_quota_bytes, models.User.used_bytes,
                  models.User.expire_at, models.Connection.enabled)
        .join(models.User, models.Connection.user_id == models.User.id)
        .filter(models.Connection.purchase_id.is_(None))
        .filter(models.Connection.type.in_((models.ConnectionType.wireguard, models.ConnectionType.xray)))
        .filter(models.Connection.session_limited.is_(False))
        .filter(models.User.status != models.UserStatus.disabled)
        .all()
    )
    for conn_id, quota, used, expire_at, enabled in legacy_rows:
        exceeded = bool(quota) and (used or 0) >= quota
        expired = expire_at is not None and expire_at < now
        should_be_active = not (exceeded or expired)
        if enabled != should_be_active:
            mismatched_ids.append((conn_id, should_be_active))

    if not mismatched_ids:
        return 0

    conns = {
        c.id: c for c in db.query(models.Connection)
        .filter(models.Connection.id.in_([cid for cid, _ in mismatched_ids]))
        .all()
    }
    for conn_id, should_be_active in mismatched_ids:
        conn = conns.get(conn_id)
        if conn is None:
            continue
        _set_connection_enabled(db, conn, enabled=should_be_active)
        toggled += 1
    return toggled


def _reconcile_ppp_sessions(db: Session, node: models.Node, mt: MikrotikClient) -> None:
    """RADIUS accounting alone (see radius_server.py) can leave a
    RadiusActiveSession row open for a user who isn't really connected
    anymore - most commonly right after a MikroTik import, when a PPP
    secret was already connected on the router *before* it existed as a
    Connection row here: the very first Interim-Update/Stop packet the
    panel then sees for it has no matching session, gets treated as
    "missed the Start" (see _touch_active_session), and is recorded as a
    fresh live session even though the router may have already dropped it
    long before. The same drift can happen any time a Stop packet is lost
    (crash, brief accounting hiccup) rather than only after an import.

    RouterOS's own `/ppp/active` table is the actual ground truth for who's
    connected right now, so cross-checking against it on every poll cycle -
    cheap, one extra API call per node - closes any RadiusActiveSession row
    accounting alone left dangling, instead of waiting on the 15-minute
    staleness reaper (cleanup_stale_radius_sessions) or never closing it at
    all if the router keeps sending periodic Interim-Updates for a session
    that isn't actually there."""
    ppp_conns = [c for c in node.connections if c.type in PPP_TYPES and c.ppp_username]
    if not ppp_conns:
        return
    try:
        live_usernames = mt.list_active_ppp_usernames()
    except MikrotikError as exc:
        logger.warning("mikrotik node %s: failed to read /ppp/active for reconciliation: %s", node.id, exc)
        return
    by_id = {c.id: c for c in ppp_conns}
    stale_rows = (
        db.query(models.RadiusActiveSession)
        .filter(models.RadiusActiveSession.connection_id.in_(list(by_id.keys())))
        .all()
    )
    for row in stale_rows:
        conn = by_id.get(row.connection_id)
        if conn and conn.ppp_username not in live_usernames:
            if conn.radius_session_id == row.session_id:
                conn.radius_session_id = None
            db.delete(row)


def poll_mikrotik_node(db: Session, node: models.Node):
    """Polls WireGuard peer counters. OpenVPN/L2TP (PPP) usage is no longer
    polled here - it now arrives in real time via RADIUS accounting packets
    (see services/radius_server.py), which call _apply_delta directly as
    Interim-Update/Stop packets come in.

    Even when a node has zero WireGuard peers (openvpn/l2tp-only nodes,
    which is now the common case), we still make a lightweight RouterOS API
    call here so last_seen/last_error - and therefore the "آنلاین" status
    shown on the dashboard - stay accurate. Previously this function
    returned immediately for WireGuard-less nodes without ever touching
    last_seen, so such nodes always showed as offline regardless of their
    real status."""
    wg_conns = [c for c in node.connections if c.type == models.ConnectionType.wireguard]
    try:
        with MikrotikClient.for_node(node) as mt:
            _reconcile_ppp_sessions(db, node, mt)
            peers = mt.list_peers(node.mt_wireguard_interface if wg_conns else None)
            if wg_conns:
                by_comment = {p.get("comment"): p for p in peers if p.get("comment")}
                for conn in wg_conns:
                    peer = by_comment.get(conn.wg_peer_name)
                    if not peer:
                        conn.online = False
                        continue
                    rx = int(peer.get("rx", 0) or 0)
                    tx = int(peer.get("tx", 0) or 0)
                    _apply_delta(db, conn, rx, tx)
                    # RouterOS reports the peer's live UDP endpoint (address
                    # only, without :port) here while a handshake is fresh -
                    # this is the closest thing WireGuard has to a "client
                    # IP" (there's no separate tunnel-internal login event
                    # like PPP/RADIUS). Cleared to None once the peer goes
                    # stale below so a disconnected peer doesn't keep
                    # showing a stale IP.
                    endpoint = peer.get("current-endpoint-address") or None
                    conn.last_client_ip = endpoint.split(":")[0] if endpoint else None
                    # RouterOS has no explicit online/offline flag for
                    # WireGuard - only a "how long since last handshake"
                    # duration, which we treat as "currently connected" if
                    # recent enough. Feeds both the آنلاین/آفلاین badge
                    # (UserDetail.jsx already renders it for every
                    # connection type) and the cross-protocol concurrent-
                    # session count in radius_server.py.
                    age = parse_ros_duration_seconds(peer.get("last-handshake"))
                    conn.online = age is not None and age <= WIREGUARD_ONLINE_THRESHOLD_SECONDS

        node.last_seen = dt.datetime.utcnow()
        node.last_error = None
    except MikrotikError as exc:
        node.last_error = str(exc)
        logger.warning("mikrotik node %s error: %s", node.id, exc)


def poll_xray_node(db: Session, node: models.Node):
    connections = [c for c in node.connections if c.type == models.ConnectionType.xray]
    if not connections:
        return
    try:
        with client_for_node(node) as xc:
            stats = xc.query_all_user_stats()
            for conn in connections:
                bucket = stats.get(conn.xr_email)
                if not bucket:
                    continue
                _apply_delta(db, conn, bucket.get("downlink", 0), bucket.get("uplink", 0))

            # Live online/offline flag (3X-UI only - see
            # ThreeXUIClient.get_online_emails; SSH-managed nodes always get
            # back an empty "unknown" set here, so their connections simply
            # stay marked offline, same as before this feature existed).
            online_emails = xc.get_online_emails()
            for conn in connections:
                conn.online = bool(conn.xr_email) and conn.xr_email in online_emails
                if conn.online:
                    # Mirrors radius_server.py's "count validity from first
                    # use" activation - PPP logins go through RADIUS, which
                    # has its own copy of this check, but xray/VLESS has no
                    # such login event, so a xray-only user's
                    # expire_days_after_first_use would otherwise never
                    # fire. Being seen online here is the xray equivalent of
                    # "first successful login". Idempotent: once activated,
                    # expire_days_after_first_use is cleared to None, so
                    # this is a no-op on every later poll.
                    user = conn.user
                    if user.expire_at is None and user.expire_days_after_first_use:
                        user.expire_at = dt.datetime.utcnow() + dt.timedelta(days=user.expire_days_after_first_use)
                        user.expire_days_after_first_use = None
                        logger.info(
                            "xray poll: activated first-use expiry for user=%r -> expire_at=%s",
                            user.username, user.expire_at.isoformat(),
                        )
        node.last_seen = dt.datetime.utcnow()
        node.last_error = None
    except XrayError as exc:
        node.last_error = str(exc)
        logger.warning("xray node %s error: %s", node.id, exc)


def poll_all():
    db = SessionLocal()
    try:
        nodes = db.query(models.Node).filter(models.Node.enabled == True).all()  # noqa: E712
        for node in nodes:
            # Each node gets its own try/except + commit. Before this fix,
            # ANY uncaught exception from a single node - poll_mikrotik_node/
            # poll_xray_node only catch their own MikrotikError/XrayError,
            # so a raw exception from lower down (e.g. mikrotik_client.py
            # or the xray client) propagated straight out of this loop -
            # hit the blanket `except Exception` far below and rolled back
            # the ENTIRE cycle's session in one shot, silently discarding
            # every usage/billing/enforcement change already made for every
            # node processed earlier in the SAME cycle, just because one
            # later router was flaky. Committing per node means one bad
            # node now costs exactly that node's data for this cycle -
            # nothing more (found during the 2026-09 full-codebase audit).
            try:
                if node.type == models.NodeType.mikrotik:
                    poll_mikrotik_node(db, node)
                elif node.type == models.NodeType.xray:
                    poll_xray_node(db, node)
                db.commit()
            except Exception:
                logger.exception("poll_all: node %s failed, rolling back only this node's changes", node.id)
                db.rollback()

        # selectinload, not lazy loading: both enforce functions walk
        # `.connections`, so plain .all() issued one extra SELECT per user and
        # per purchase - ~1,200 queries every poll. At the default 30s
        # interval and the current 609 customers that is roughly 2.3 million
        # redundant queries a day, the same shape of problem as the 3X-UI
        # per-client polling storm. selectinload is SQLAlchemy's recommended
        # strategy for one-to-many collections (a JOIN would duplicate parent
        # rows); this turns the whole loop into 2 queries per collection.
        # Only the rows that can actually change are loaded as ORM objects.
        #
        # This loop used to load EVERY user, purchase and connection on the
        # panel every 30 seconds - about 75,000 objects at 20,000
        # customers - to discover that almost none of them needed anything.
        # Measured at 3.17s and 260MB per cycle standing still, and 20.9s
        # when admins were browsing at the same time (backend/tests/
        # stress_background.py + stress_concurrent.py). At a 30s interval
        # that is a cycle that barely finishes before the next one starts.
        #
        # The two helpers above run the same rules over plain tuples first
        # and hand back only the ids worth loading. Everything below this
        # point is unchanged - same functions, same order, same results.
        now = dt.datetime.utcnow()

        # Purchases BEFORE users, which is also a small correctness win: a
        # customer's account badge is derived from their services' statuses
        # (_user_status_from_purchases), so doing services first means the
        # badge reflects this cycle's verdict instead of the last one's.
        purchase_ids = _purchase_ids_needing_enforcement(db, now)
        if purchase_ids:
            purchases = (
                db.query(models.Purchase)
                .options(selectinload(models.Purchase.connections))
                .filter(models.Purchase.id.in_(purchase_ids))
                .all()
            )
            for purchase in purchases:
                _enforce_purchase_limits(db, purchase)
            # SessionLocal has autoflush=False, so without this the user
            # pass below would still read the OLD purchase statuses.
            db.flush()

        user_ids = _user_ids_needing_enforcement(db, now)
        if user_ids:
            users = (
                db.query(models.User)
                .options(
                    selectinload(models.User.connections),
                    selectinload(models.User.purchases),
                )
                .filter(models.User.id.in_(user_ids))
                .all()
            )
            for user in users:
                _enforce_user_limits(db, user)

        # Last, and deliberately after both enable/disable passes above:
        # reactively kicks (or releases) a WireGuard/Xray connection over a
        # user's own concurrent-session cap - needs this cycle's freshly
        # polled Connection.online values from the node loop at the top of
        # this function, and needs to run after quota enforcement so
        # _apply_enabled_state's session_limited override actually has
        # something to override rather than racing it. See its own
        # docstring for the full "why" (2026-09-06).
        session_limited_count = enforce_concurrent_session_limits(db)

        # Independent of everything above: catches any WireGuard/Xray
        # connection whose enabled flag never actually caught up with its
        # governing quota/expiry - see _reconcile_connection_enabled_state's
        # own docstring for why this is needed even with the rest of this
        # function working correctly (2026-09 bug report).
        reconciled_count = _reconcile_connection_enabled_state(db, now)

        if purchase_ids or user_ids or session_limited_count or reconciled_count:
            logger.info(
                "poll_all: enforced %d purchase(s), %d account(s), %d concurrent-session toggle(s), "
                "%d reconciled connection(s)",
                len(purchase_ids), len(user_ids), session_limited_count, reconciled_count,
            )

        db.commit()
    except Exception:
        logger.exception("poll_all failed")
        db.rollback()
    finally:
        db.close()
