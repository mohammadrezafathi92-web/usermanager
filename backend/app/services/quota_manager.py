"""Background job: pull traffic counters from every node, add the deltas
onto each connection's owning user (shared quota across all protocols),
and enable/disable connections as quota / expiry dictate."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import time
import logging

from sqlalchemy import update
from sqlalchemy.orm import Session, selectinload, joinedload
from fastapi import HTTPException

from .. import models
from ..database import SessionLocal
from .mikrotik_client import MikrotikClient, MikrotikError, parse_ros_duration_seconds
from .node_gate import writer_gate
from . import legacy_remote, session_cap_mode, session_cap_rule
from . import license_guard, license_fence
from .user_ops import _maybe_activate_reserved_renewal, _maybe_activate_reserved_purchase_renewal
from .xray_client import XrayError, client_for_node
from .softether_client import SoftEtherError, client_for_node as softether_client_for_node

logger = logging.getLogger("quota_manager")
_license_node_retry_after = {}
LICENSE_TRANSITIONS_PER_PASS = 16

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


def _apply_delta(db: Session, connection: models.Connection, download_bytes: int, upload_bytes: int):
    """Given fresh cumulative download/upload counters from the node,
    compute the delta since last poll for EACH direction independently
    (handling counter resets per-counter, not combined - see below) and add
    the total to the owning allotment's usage - the connection's OWN
    Purchase (see models.Purchase's docstring) if it has one, or the owning
    User's combined used_bytes otherwise (the original, still-default
    behavior for every connection not created via the "افزودن پکیج" flow).

    Callers must pass true client-facing directions, not whatever a node
    happens to call its two counters - see each call site's comment for how
    that protocol's raw rx/tx/uplink/downlink maps onto these two. Getting
    this backwards doesn't break totals (delta_bytes is still the sum
    either way) but silently swaps every upload/download split shown on the
    dashboard and per-user usage charts.

    Deltas are computed per-counter now instead of on the combined total -
    the previous combined check (comparing rx+tx new vs old as one number)
    missed a real case: if one counter alone reset (peer recreated mid-
    session) while the other kept growing, the combined total could still
    look like it only grew, and the reset counter's whole new value got
    silently added on top of the other's real delta - a one-time inflated
    spike. Each counter resetting on its own is now caught independently.
    """
    prev_download = connection.last_rx_bytes or 0
    prev_upload = connection.last_tx_bytes or 0
    download_bytes = download_bytes or 0
    upload_bytes = upload_bytes or 0

    download_delta = download_bytes - prev_download if download_bytes >= prev_download else download_bytes
    upload_delta = upload_bytes - prev_upload if upload_bytes >= prev_upload else upload_bytes

    connection.last_rx_bytes = download_bytes
    connection.last_tx_bytes = upload_bytes

    download_delta = max(0, download_delta)
    upload_delta = max(0, upload_delta)
    delta = download_delta + upload_delta
    if delta <= 0:
        return
    add_usage(db, connection, delta, download_delta, upload_delta)


def add_usage(db: Session, connection: models.Connection, delta: int, download_delta: int = 0, upload_delta: int = 0) -> None:
    """Add `delta` bytes to everything that counts them.

    The second half of _apply_delta, split out because the two halves have
    different owners. Working out the delta needs a baseline, and WHERE
    that baseline lives depends on the protocol: for WireGuard and Xray it
    is the connection (one counter per peer, which is what the node
    reports), and for the RADIUS protocols it is the SESSION - each session
    has its own counter starting at zero, and a connection may have several
    at once. Keeping the arithmetic here and the baseline with whoever owns
    it is what stopped two concurrent sessions from resetting each other's
    (see models.RadiusActiveSession.last_rx_bytes).

    download_delta/upload_delta are optional and only feed UsageLog's split
    columns (dashboard/per-user usage-chart tabs) - every quota/billing
    figure below still only cares about the combined `delta`, so a caller
    that can't tell the two apart can leave them at 0 and everything except
    the chart split keeps working exactly as before.
    """
    if delta <= 0:
        return
    connection.total_bytes = (connection.total_bytes or 0) + delta

    user: models.User = connection.user
    if connection.purchase_id:
        # Atomic - see _atomic_increment's docstring: a Purchase can have
        # more than one bundled connection, so its used_bytes is a shared
        # target too, not just the User-level field below.
        _atomic_increment(db, models.Purchase, connection.purchase_id, "used_bytes", delta)
    else:
        _atomic_increment(db, models.User, user.id, "used_bytes", delta)

    db.add(models.UsageLog(
        user_id=user.id, connection_id=connection.id, delta_bytes=delta,
        upload_bytes=upload_delta, download_bytes=download_delta,
    ))

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
        gb = delta / (1024 ** 3)
        _atomic_increment(db, models.AdminUser, admin.id, "volume_balance_gb", -gb)
        # ...and the same GB onto the un-priced meter, which
        # services/usage_billing.py drains once a day into a single priced
        # admin_usage_charge ledger row. Depleting the GB pool alone (all
        # this used to do) meant a usage-billed reseller's traffic never
        # became money anywhere in the books - their cost of goods read as
        # zero and every margin shown for them was 100% (bug found
        # 2026-09).
        _atomic_increment(db, models.AdminUser, admin.id, "unbilled_usage_gb", gb)


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

    if (exceeded or expired) and _maybe_activate_reserved_purchase_renewal(db, purchase, "poller"):
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


def _enforce_legacy_session_limits(db: Session) -> int:
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



def enforce_concurrent_session_limits(db: Session, mode: str) -> int:
    """The poll cycle's concurrent-session step, by session_cap_mode `mode`:
    legacy and shadow act exactly as _enforce_legacy_session_limits always
    did; shadow also records what the per-Purchase rule would have done
    instead (nothing more); enforced acts per Purchase
    (session_cap_rule.session_scope). The caller holds
    session_cap_mode.decision - the one that yielded `mode` - until it has
    committed what this did (_finish_cycle)."""
    if mode == "enforced":
        return _enforce_per_purchase_session_limits(db)
    if mode == "shadow":
        try:
            _shadow_per_purchase_session_limits(db)
        except Exception:  # noqa: BLE001 - a shadow record never touches enforcement
            logger.exception("session cap shadow comparison failed")
    elif mode != "legacy":
        raise ValueError(f"unknown session cap mode {mode!r}")
    return _enforce_legacy_session_limits(db)


def _per_purchase_candidates(db: Session):
    """Every user the per-Purchase rule could act on: a User cap, a capped
    Purchase, or a connection still kicked (to be released)."""
    ids = {
        uid for (uid,) in db.query(models.User.id)
        .filter(models.User.max_concurrent_sessions.isnot(None), models.User.max_concurrent_sessions > 0)
        .all()
    } | {
        uid for (uid,) in db.query(models.Purchase.user_id)
        .filter(models.Purchase.max_concurrent_sessions.isnot(None), models.Purchase.max_concurrent_sessions > 0)
        .distinct()
        .all()
    } | {
        uid for (uid,) in db.query(models.Connection.user_id)
        .filter(models.Connection.session_limited.is_(True))
        .distinct()
        .all()
    }
    if not ids:
        return []
    return (
        db.query(models.User)
        .options(selectinload(models.User.connections).selectinload(models.Connection.purchase))
        .filter(models.User.id.in_(ids))
        .all()
    )


def _enforce_per_purchase_session_limits(db: Session) -> int:
    """Each scope (session_cap_rule.session_scope) decides on its own
    connections only: a kick or a release in one Purchase never makes or
    takes room in another, nor in the customer's legacy pool."""
    users = _per_purchase_candidates(db)
    counts = session_cap_rule.ppp_counts(db, [u.id for u in users])
    toggled = 0
    for user in users:
        kicks, releases = session_cap_rule.plan(user, session_cap_rule.capped_scopes(user), counts)
        for conn, _limit, _count in kicks:
            conn.session_limited = True
            _set_connection_enabled(db, conn, enabled=False)
            toggled += 1
        for conn, _limit, _count in releases:
            conn.session_limited = False
            _set_connection_enabled(db, conn, enabled=True)
            toggled += 1
    return toggled


def _shadow_per_purchase_session_limits(db: Session) -> None:
    """Compares, before legacy acts, what each rule would kick and release
    this cycle and records every connection where they disagree. Reads
    only."""
    users = _per_purchase_candidates(db)
    counts = session_cap_rule.ppp_counts(db, [u.id for u in users])
    for user in users:
        legacy_kicks, legacy_releases = session_cap_rule.plan(user, session_cap_rule.legacy_scopes(user), counts)
        new_kicks, new_releases = session_cap_rule.plan(user, session_cap_rule.capped_scopes(user), counts)
        # (kind, the rule that acts, its plan, the other rule's plan)
        for kind, new_side, acting, other in (("poll_would_kick", True, new_kicks, legacy_kicks),
                                              ("poll_would_not_kick", False, legacy_kicks, new_kicks),
                                              ("poll_would_release", True, new_releases, legacy_releases),
                                              ("poll_would_not_release", False, legacy_releases, new_releases)):
            others = {conn.id for conn, _limit, _count in other}
            for conn, limit, count in acting:
                if conn.id in others:
                    continue
                session_cap_mode.note(kind, user_id=user.id, purchase_id=conn.purchase_id, connection_id=conn.id,
                                      legacy=(None, None) if new_side else (limit, count),
                                      new=(limit, count) if new_side else (None, None))


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
    # Licence suspension has its own marker; ordinary quota/session passes
    # must not reopen a cut peer or clear that marker behind the operator.
    if connection.license_suspended:
        # Preserve the pre-cut connection switch while its remote state is
        # owned by licensing. Eligibility still reads current quota/expiry
        # and session limits; quota expiry cannot strand a later renewal.
        return
    if enabled and license_guard.current().cut_services:
        return
    # Whether or not the feature is on now: a pending cancellation owns
    # its service's stop/delete; nothing re-enables (or toggles) it.
    from .reseller_refund_block import purchase_pending
    if connection.purchase_id and purchase_pending(db, connection.purchase_id):
        return
    if connection.enabled == enabled:
        return
    node: models.Node = connection.node
    applied = True  # PPP has nothing to push to the node - the DB flag alone is the switch
    try:
        if connection.type == models.ConnectionType.wireguard:
            if not legacy_remote.run(node, "wg_set_enabled", factory=MikrotikClient.for_node, interface=node.mt_wireguard_interface,
                                     peer_name=connection.wg_peer_name, enabled=enabled):
                applied = False
                logger.warning(
                    "wireguard peer not found on node %s for connection %s (wg_peer_name=%r) - "
                    "could not %s it; will retry next poll cycle",
                    node.id, connection.id, connection.wg_peer_name,
                    "enable" if enabled else "disable",
                )
        elif connection.type in PPP_TYPES:
            # Authenticated via RADIUS - the RADIUS auth handler checks
            # connection.enabled on every NEW Access-Request. Existing
            # sessions must also be closed: the poll cycle uses RouterOS's
            # live session list after all quota/renewal decisions below.
            pass
        elif connection.type == models.ConnectionType.xray:
            applied = legacy_remote.run(node, "xray_set_enabled", factory=client_for_node, inbound_tag=node.xr_inbound_tag,
                                        email=connection.xr_email, client_uuid=connection.xr_uuid,
                                        flow=connection.xr_flow or "", enabled=enabled)
            if not applied:
                logger.warning(
                    "xray client could not be %s on node %s for connection %s (xr_email=%r); "
                    "will retry next poll cycle",
                    "enabled" if enabled else "disabled", node.id, connection.id, connection.xr_email,
                )
        elif connection.type == models.ConnectionType.softether:
            applied = legacy_remote.run(node, "softether_set_enabled", factory=softether_client_for_node, username=connection.ppp_username,
                                        password=connection.ppp_password, enabled=enabled)
            if not applied:
                logger.warning(
                    "softether user could not be %s on node %s for connection %s (ppp_username=%r); "
                    "will retry next poll cycle",
                    "enabled" if enabled else "disabled", node.id, connection.id, connection.ppp_username,
                )
        if applied:
            connection.enabled = enabled
    except (MikrotikError, XrayError, SoftEtherError) as exc:
        logger.warning("failed to toggle connection %s: %s", connection.id, exc)


def _license_connection(db: Session, connection_id: int):
    return db.query(models.Connection).options(
        joinedload(models.Connection.node), joinedload(models.Connection.user),
        joinedload(models.Connection.purchase),
    ).filter(models.Connection.id == connection_id).one_or_none()


def _license_remote_state(conn: models.Connection, enabled: bool) -> bool:
    """Detached snapshot only: caller has closed its database transaction.

    Every mutation still passes through the existing ownership/node runner.
    PPP stop preserves accounting rows/counters for the final Acct-Stop.
    """
    node = conn.node
    if conn.type in PPP_TYPES or conn.type == models.ConnectionType.pptp:
        if not enabled:
            legacy_remote.run(node, "ppp_kick", factory=MikrotikClient.for_node,
                              username=conn.ppp_username, hard_deadline_seconds=3)
        return True  # False from ppp_kick is a verified already-absent session.
    if conn.type == models.ConnectionType.wireguard:
        return bool(legacy_remote.run(node, "wg_set_enabled", factory=MikrotikClient.for_node,
            interface=node.mt_wireguard_interface, peer_name=conn.wg_peer_name, enabled=enabled, hard_deadline_seconds=3))
    if conn.type == models.ConnectionType.xray:
        return bool(legacy_remote.run(node, "xray_set_enabled", factory=client_for_node,
            inbound_tag=node.xr_inbound_tag, email=conn.xr_email, client_uuid=conn.xr_uuid,
            flow=conn.xr_flow or "", enabled=enabled, hard_deadline_seconds=3))
    if conn.type == models.ConnectionType.softether:
        return bool(legacy_remote.run(node, "softether_set_enabled", factory=softether_client_for_node,
            username=conn.ppp_username, password=conn.ppp_password, enabled=enabled, hard_deadline_seconds=3))
    return False


def _license_target_hash(conn):
    """Bind a suspension to its customer, credential and node endpoint.

    Ordinary quota/expiry/usage updates are deliberately not identity changes.
    Never return this internal fingerprint or any credential in public output.
    """
    target = (conn.id, conn.created_at, conn.user_id, conn.user.created_at,
              conn.user.owner_admin_id, conn.user.username, conn.node_id, conn.type.value,
              conn.purchase_id, conn.purchase.created_at if conn.purchase is not None else None,
              legacy_remote.endpoint(conn.node), conn.wg_peer_name,
              conn.ppp_username, conn.ppp_password, conn.xr_email, conn.xr_uuid, conn.xr_flow)
    return hashlib.sha256(json.dumps(target, default=str, sort_keys=True).encode()).hexdigest()


def enforce_license_services() -> dict:
    """One short-lived exclusive fence per connection, never a whole batch."""
    with SessionLocal() as db:
        if not license_fence.ready(db.get_bind()):
            raise HTTPException(503, "license_runtime_fence_unavailable")
        panel = db.get(models.PanelSettings, 1)
        if panel is not None and panel.ha_enabled:
            raise HTTPException(409, "license_runtime_requires_single_host")
        query = db.query(models.Connection.id, models.Connection.node_id)
        if license_guard.current().cut_services:
            query = query.filter(
                models.Connection.enabled.is_(True) | models.Connection.license_suspended.is_(True),
                models.Connection.license_suspended.is_(False) | models.Connection.license_cut_applied.is_(False))
        else:
            query = query.filter(models.Connection.license_suspended.is_(True))
        ids = query.order_by(models.Connection.id).all()
    totals = {"cut_services": license_guard.current().cut_services, "applied": 0, "restoration_pending": 0}
    attempted = 0
    started = time.monotonic()
    for cid, node_id in ids:
        if (_license_node_retry_after.get(node_id, 0) > time.monotonic() or
                attempted >= LICENSE_TRANSITIONS_PER_PASS or time.monotonic() - started >= 10):
            totals["restoration_pending"] += 1
            continue
        try:
            with license_fence.exclusive():
                result = _enforce_license_services([cid])
            attempted += result["remote_attempted"]
            for key in ("applied", "restoration_pending"):
                totals[key] += result[key]
        except HTTPException:
            totals["restoration_pending"] += 1
    return totals


def _enforce_license_services(ids) -> dict:
    """Separate, retryable stop pass; no DB transaction survives node I/O.

    Renewal retries eligible connections under the installation fence.
    No customer transaction or node database transaction spans remote I/O.
    """
    cut = license_guard.current().cut_services
    applied = pending = remote_attempted = 0
    for cid in ids:
        with SessionLocal() as db:
            conn = _license_connection(db, cid)
            if conn is None:
                continue
            panel = db.get(models.PanelSettings, 1)
            if panel is not None and panel.ha_enabled:
                raise HTTPException(409, "license_runtime_requires_single_host")
            if not cut:
                suspended = conn.license_suspended
                db.close()
                if suspended:
                    try:
                        restore_license_connection(cid)
                        applied += 1
                        remote_attempted += 1
                    except HTTPException as exc:
                        if exc.detail in ("license_restore_unverified", "license_restore_state_changed"):
                            remote_attempted += 1
                            _license_node_retry_after[conn.node_id] = time.monotonic() + 60
                        pending += 1
                continue
            if not conn.enabled and not conn.license_suspended:
                continue
            if conn.license_suspended and conn.license_cut_applied:
                continue
            if not conn.license_suspended:
                conn.license_target_hash = _license_target_hash(conn)
            elif conn.license_target_hash != _license_target_hash(conn):
                pending += 1
                continue
            conn.license_suspended = True
            db.flush()
            # Fully load and detach before commit expiration/closing; node
            # credentials never enter a public result or an error message.
            node = conn.node
            db.expunge(conn)
            db.expunge(node)
            db.commit()
        if not node.enabled:
            pending += 1
            continue
        # A renewed licence can arrive while a large stop pass is running.
        # Do not continue cutting the rest of the installation on its old
        # verdict. Keep the marker for explicit outcome reconciliation.
        if not license_guard.current().cut_services:
            pending += 1
            continue
        try:
            remote_attempted += 1
            stopped = _license_remote_state(conn, False)
        except Exception:
            # Ownership unavailable, timeout and unknown are all pending,
            # never recorded as a known remote stop.
            logger.warning("licence suspension pending for connection %s", cid)
            stopped = False
        if not stopped:
            _license_node_retry_after[node.id] = time.monotonic() + 60
            pending += 1
            continue
        with SessionLocal() as db:
            db.query(models.Connection).filter_by(id=cid, license_suspended=True).update(
                {"license_cut_applied": True}, synchronize_session=False)
            db.commit()
        applied += 1
    return {"cut_services": cut, "applied": applied, "restoration_pending": pending,
            "remote_attempted": remote_attempted}


def _license_restore_snapshot(conn: models.Connection, now: dt.datetime):
    user, purchase = conn.user, conn.purchase
    if not conn.enabled or conn.session_limited or not conn.node.enabled:
        return None
    if user.status == models.UserStatus.disabled:
        return None
    subject = purchase if purchase is not None else user
    quota = purchase.quota_bytes if purchase is not None else user.total_quota_bytes
    if subject.status != models.UserStatus.active or (quota and subject.used_bytes >= quota):
        return None
    if subject.expire_at is not None and subject.expire_at <= now:
        return None
    return (conn.created_at, conn.user_id, user.created_at, user.owner_admin_id, user.username,
            conn.enabled, conn.session_limited, conn.node_id, conn.type, conn.purchase_id,
            user.status, subject.status, quota, subject.used_bytes,
            subject.expire_at, conn.wg_peer_name, conn.ppp_username, conn.ppp_password,
            conn.xr_uuid, conn.xr_email, conn.xr_flow)


def restore_license_connection(connection_id: int, *, expected_target=None) -> dict:
    """Automatic or authenticated recovery; serialize before the first read."""
    with SessionLocal() as probe:
        bind = probe.get_bind()
    if not license_fence.ready(bind):
        raise HTTPException(503, "license_runtime_fence_unavailable")
    with license_fence.exclusive():
        return _restore_license_connection(connection_id, expected_target=expected_target)


def _restore_license_connection(connection_id: int, *, expected_target=None) -> dict:
    if license_guard.current().cut_services:
        raise HTTPException(409, "license_services_still_suspended")
    with SessionLocal() as db:
        conn = _license_connection(db, connection_id)
        if conn is None:
            raise HTTPException(404, "connection_not_found")
        target = (conn.created_at, conn.user_id, conn.user.created_at, conn.user.owner_admin_id, conn.user.username)
        if expected_target is not None and target != expected_target:
            raise HTTPException(409, "license_restore_target_changed")
        if not conn.license_suspended:
            return {"restored": False, "reason": "not_license_suspended"}
        if conn.license_target_hash != _license_target_hash(conn):
            raise HTTPException(409, "license_restore_target_changed")
        _license_restore_holds(db, conn)
        snapshot = _license_restore_snapshot(conn, dt.datetime.utcnow())
        if snapshot is None:
            raise HTTPException(409, "license_restore_not_eligible")
        db.expunge_all()
    try:
        enabled = _license_remote_state(conn, True)
    except Exception:
        enabled = False
    with SessionLocal() as db:
        current = _license_connection(db, connection_id)
        held = False
        if current is not None:
            try:
                _license_restore_holds(db, current)
            except HTTPException:
                held = True
        valid = enabled and not held and not license_guard.current().cut_services and current is not None and (
            current.license_target_hash == _license_target_hash(current)) and (
            _license_restore_snapshot(current, dt.datetime.utcnow()) == snapshot)
        if valid:
            current.license_suspended = False
            current.license_cut_applied = False
            current.license_target_hash = None
            db.commit()
            return {"restored": True, "connection_id": connection_id}
    # Even an unknown enable can have reached the node. Re-cut outside the
    # DB transaction and retain the marker irrespective of its outcome.
    try:
        stopped = _license_remote_state(conn, False)
    except Exception:
        stopped = False
    with SessionLocal() as db:
        db.query(models.Connection).filter_by(id=connection_id, license_suspended=True).update(
            {"license_cut_applied": bool(stopped)}, synchronize_session=False)
        db.commit()
    raise HTTPException(409 if enabled else 503, "license_restore_state_changed" if enabled else "license_restore_unverified")


def _license_restore_holds(db, conn):
    from . import provisioning_schema, receipt_void_schema, reservation_transition
    if not provisioning_schema.is_ready() or not receipt_void_schema.is_ready():
        raise HTTPException(503, "license_restore_schema_not_ready")
    if db.get(models.PanelSettings, 1) is not None and db.get(models.PanelSettings, 1).ha_enabled:
        raise HTTPException(409, "license_restore_requires_single_host")
    if conn.purchase is not None and receipt_void_schema.is_ready() and reservation_transition.is_held(
            db, conn.purchase.history_key):
        raise HTTPException(409, "purchase_void_in_progress")
    if provisioning_schema.is_ready():
        from ..models_provisioning import ProvisioningOperation
        if db.query(ProvisioningOperation.id).filter(
                (ProvisioningOperation.target_user_id == conn.user_id) |
                (ProvisioningOperation.result_user_id == conn.user_id),
                ProvisioningOperation.state.notin_(("completed", "compensated"))).first():
            # Accepted durable work can be between DB transactions while
            # its child owns remote I/O. Let it settle before restoration.
            raise HTTPException(409, "license_restore_operation_pending")
    # Flag on or off: a pending cancellation owns its service until settled.
    from ..models_reseller_refund import ResellerRefundOperation
    if db.query(ResellerRefundOperation.id).filter_by(user_id=conn.user_id, state="pending").first():
        raise HTTPException(409, "reseller_cancellation_pending")



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
        .filter(models.Connection.type.in_((models.ConnectionType.wireguard, models.ConnectionType.xray, models.ConnectionType.softether)))
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
        .filter(models.Connection.type.in_((models.ConnectionType.wireguard, models.ConnectionType.xray, models.ConnectionType.softether)))
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
    db.info.setdefault("quota_ppp_live", {})[node.id] = set(live_usernames)
    # A connection held by an unpaid-service cancellation keeps its rows:
    # gone from /ppp/active is not its Accounting Stop, and only the Stop
    # proves the final usage (reseller_refund_block).
    from .reseller_refund_block import blocked_connection_ids
    held = blocked_connection_ids(db)
    by_id = {c.id: c for c in ppp_conns if c.id not in held}
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


def _disconnect_blocked_ppp_sessions(db: Session) -> int:
    """Close live PPP sessions after quota/expiry and queued renewals settle.

    A disabled database flag only rejects the NEXT RADIUS authentication;
    it does not terminate a session already carrying traffic. Retry from
    fresh router observations every poll, including sessions whose Start
    accounting packet was lost. Use the existing fenced P6 runner action,
    never raw client writes. Failed/unknown remote outcomes retain the
    session rows so a later poll can retry.
    """
    live_by_node = db.info.pop("quota_ppp_live", {})
    if not live_by_node:
        return 0
    # autoflush is disabled: the enforcement passes just changed flags.
    db.flush()
    connections = db.query(models.Connection).filter(
        models.Connection.type.in_(PPP_TYPES), models.Connection.enabled.is_(False),
        models.Connection.node_id.in_(list(live_by_node)),
    ).all()
    stopped = 0
    for conn in connections:
        live = live_by_node[conn.node_id]
        if not conn.ppp_username or conn.ppp_username not in live:
            continue
        try:
            legacy_remote.run(conn.node, "ppp_kick", factory=MikrotikClient.for_node,
                              username=conn.ppp_username)
        except MikrotikError:
            logger.warning("quota cutoff: PPP disconnect failed for connection %s; retry next poll", conn.id)
            continue
        # False means the session already disappeared: also verified absent.
        # Keep per-session counters until Accounting-Stop (or the normal
        # router reconciliation). Deleting them now would make a late Stop
        # re-add the session's entire cumulative traffic as new usage.
        conn.online = False
        live.discard(conn.ppp_username)
        stopped += 1
    return stopped


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
                    # RouterOS reports these from the router's own point of
                    # view: "rx" is what it received FROM the peer (the
                    # client's upload), "tx" is what it sent TO the peer
                    # (the client's download) - the opposite of _apply_delta's
                    # (download, upload) parameter order, so these are
                    # passed in swapped.
                    rx = int(peer.get("rx", 0) or 0)
                    tx = int(peer.get("tx", 0) or 0)
                    _apply_delta(db, conn, download_bytes=tx, upload_bytes=rx)
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
                    peer_disabled = peer.get("disabled") in (True, "true", "yes", "1", 1)
                    conn.online = not peer_disabled and age is not None and age <= WIREGUARD_ONLINE_THRESHOLD_SECONDS

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
                # xray-core's own naming already matches _apply_delta's
                # (download, upload) order: downlink is server->client
                # (download), uplink is client->server (upload).
                _apply_delta(db, conn, download_bytes=bucket.get("downlink", 0), upload_bytes=bucket.get("uplink", 0))

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


def poll_softether_node(db: Session, node: models.Node):
    """Mirrors poll_xray_node exactly - one bulk EnumUser call for traffic,
    one bulk EnumSession call for the live online set, keyed by username
    instead of email."""
    connections = [c for c in node.connections if c.type == models.ConnectionType.softether]
    if not connections:
        return
    try:
        with softether_client_for_node(node) as sc:
            stats = sc.query_all_user_stats()
            for conn in connections:
                bucket = stats.get(conn.ppp_username)
                if not bucket:
                    continue
                # SoftEtherClient's own naming already matches _apply_delta's
                # (download, upload) order - see its query_all_user_stats
                # docstring for the Recv/Send -> uplink/downlink mapping.
                _apply_delta(db, conn, download_bytes=bucket.get("downlink", 0), upload_bytes=bucket.get("uplink", 0))

            online_usernames = sc.get_online_emails()
            for conn in connections:
                conn.online = bool(conn.ppp_username) and conn.ppp_username in online_usernames
                if conn.online:
                    # Same first-use activation as poll_xray_node - a
                    # SoftEther login has no separate RADIUS-style event of
                    # its own, so being seen online here is it.
                    user = conn.user
                    if user.expire_at is None and user.expire_days_after_first_use:
                        user.expire_at = dt.datetime.utcnow() + dt.timedelta(days=user.expire_days_after_first_use)
                        user.expire_days_after_first_use = None
                        logger.info(
                            "softether poll: activated first-use expiry for user=%r -> expire_at=%s",
                            user.username, user.expire_at.isoformat(),
                        )
        node.last_seen = dt.datetime.utcnow()
        node.last_error = None
    except SoftEtherError as exc:
        node.last_error = str(exc)
        logger.warning("softether node %s error: %s", node.id, exc)


from .reseller_refund_fence import guarded as _refund_guarded


@_refund_guarded
def _finish_cycle(db: Session, now, purchase_ids, user_ids) -> None:
    """poll_all's last steps and its commit, under session_cap_mode's shared
    lock from before the concurrent-session step reads the mode until after
    the commit - a mode change can never fall between a kick or release
    and its being committed. No valid mode: that step is skipped this cycle
    (no kick, no release); everything else proceeds as before."""
    with contextlib.ExitStack() as hold:
        try:
            cap_mode = hold.enter_context(session_cap_mode.decision(db.get_bind()))
        except session_cap_mode.ModeUnavailable as exc:
            cap_mode = None
            logger.error("poll_all: concurrent-session step skipped this cycle: no session cap mode (%s)", exc)
        session_limited_count = enforce_concurrent_session_limits(db, cap_mode) if cap_mode else 0

        # Independent of everything above: catches any WireGuard/Xray
        # connection whose enabled flag never actually caught up with its
        # governing quota/expiry - see _reconcile_connection_enabled_state's
        # own docstring for why this is needed even with the rest of this
        # function working correctly (2026-09 bug report).
        reconciled_count = _reconcile_connection_enabled_state(db, now)
        stopped_ppp = _disconnect_blocked_ppp_sessions(db)
        if stopped_ppp:
            logger.info("poll_all: closed %d blocked PPP connection(s)", stopped_ppp)

        if purchase_ids or user_ids or session_limited_count or reconciled_count:
            logger.info(
                "poll_all: enforced %d purchase(s), %d account(s), %d concurrent-session toggle(s), "
                "%d reconciled connection(s)",
                len(purchase_ids), len(user_ids), session_limited_count, reconciled_count,
            )

        db.commit()


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
                elif node.type == models.NodeType.softether:
                    poll_softether_node(db, node)
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
        _finish_cycle(db, now, purchase_ids, user_ids)
    except Exception:
        logger.exception("poll_all failed")
        db.rollback()
    finally:
        db.close()
    try:
        enforce_license_services()
    except Exception:
        logger.error("licence service suspension pass failed; will retry next poll")
    # Its own transaction, after the cycle's: a shadow record never delays
    # or fails enforcement (session_cap_mode.flush_shadow).
    session_cap_mode.flush_shadow(SessionLocal)
