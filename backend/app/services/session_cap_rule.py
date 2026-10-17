"""The per-Purchase concurrent-session rule (owner, 2026-10-17): every
Purchase has its OWN cap - a customer's purchases are never summed, nor
does one of them (or the User) bound another. Pure: reads the ORM objects
it is given and the live session table, never writes.

Only in force in session_cap_mode 'enforced'; 'shadow' computes it next to
the legacy rule (quota_manager.active_session_count & co., unchanged) and
records where the two disagree.
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models

PPP_TYPES = (models.ConnectionType.openvpn, models.ConnectionType.l2tp, models.ConnectionType.ikev2, models.ConnectionType.sstp)


def session_scope(user: models.User, connection: models.Connection) -> tuple[int, list[models.Connection]]:
    """(cap, connections counted against it) for a session on `connection`.

      Purchase with a cap   that cap, over the connections of that Purchase
                            only; the User's cap and the connection's own
                            are not applied on top of it
      Purchase without one  the connection's own cap, over itself
      no Purchase (legacy)  as before: the User's cap if set, over the
                            customer's legacy (purchase-less) connections,
                            else the connection's own cap, over itself

    A cap of 0 means no cap."""
    if connection.purchase_id is not None:
        purchase = connection.purchase
        if purchase is not None and purchase.max_concurrent_sessions:
            return purchase.max_concurrent_sessions, [
                c for c in user.connections if c.purchase_id == connection.purchase_id]
        return connection.max_concurrent_sessions or 0, [connection]
    if user.max_concurrent_sessions:
        return user.max_concurrent_sessions, [c for c in user.connections if c.purchase_id is None]
    return connection.max_concurrent_sessions or 0, [connection]


def scope_session_count(db: Session, connections: list[models.Connection]) -> int:
    """Live sessions of these connections: PPP from RadiusActiveSession
    (real time), WireGuard/Xray from Connection.online (as fresh as the
    last poll). `online` says a config is in use, not by how many devices:
    one WireGuard peer or Xray client counts once however many share it."""
    ppp_ids = [c.id for c in connections if c.type in PPP_TYPES]
    ppp_count = (
        db.query(models.RadiusActiveSession)
        .filter(models.RadiusActiveSession.connection_id.in_(ppp_ids))
        .count()
        if ppp_ids else 0
    )
    return ppp_count + sum(1 for c in connections if c.type not in PPP_TYPES and c.online)


def capped_scopes(user: models.User) -> list[tuple[int, list[models.Connection]]]:
    """The user's connections split into the scopes session_scope defines,
    each with its cap (0 = none). Every connection is in exactly one."""
    scopes, legacy, by_purchase, singles = [], [], {}, []
    for conn in user.connections:
        if conn.purchase_id is None:
            legacy.append(conn)
        elif conn.purchase is not None and conn.purchase.max_concurrent_sessions:
            by_purchase.setdefault(conn.purchase_id, []).append(conn)
        else:
            singles.append(conn)
    for conns in by_purchase.values():
        scopes.append((conns[0].purchase.max_concurrent_sessions, conns))
    if legacy and user.max_concurrent_sessions:
        scopes.append((user.max_concurrent_sessions, legacy))
    else:
        singles.extend(legacy)
    # A connection's own cap is a per-credential PPP limit RADIUS enforces at
    # login; a WireGuard/Xray online flag cannot count devices on one config,
    # so these scopes never kick - they only release an earlier kick.
    scopes.extend((0, [conn]) for conn in singles)
    return scopes


def legacy_scopes(user: models.User) -> list[tuple[int, list[models.Connection]]]:
    """The legacy rule in the same shape (for shadow comparison only - the
    legacy rule itself runs quota_manager's own unchanged code): one scope,
    the User's cap (or none) over every connection of the customer."""
    return [(user.max_concurrent_sessions or 0, list(user.connections))]


def ppp_counts(db: Session, user_ids) -> dict[int, int]:
    """Live RADIUS sessions per connection of these users, in one query."""
    if not user_ids:
        return {}
    return dict(
        db.query(models.RadiusActiveSession.connection_id, func.count())
        .join(models.Connection, models.Connection.id == models.RadiusActiveSession.connection_id)
        .filter(models.Connection.user_id.in_(list(user_ids)))
        .group_by(models.RadiusActiveSession.connection_id)
        .all()
    )


def plan(user: models.User, scopes, counts: dict[int, int]):
    """The kicks and releases quota_manager's enforcement makes, per scope,
    without applying them: [(connection, limit, count)] for each. Same
    algorithm as the legacy one - newest online connection kicked first,
    only up to the excess; a release only for room some OTHER session left
    (the connection's own kick never counts as room, which is what keeps
    it from flapping), and never against its own quota/expiry."""
    kicks, releases = [], []
    for limit, scope in scopes:
        non_ppp = [c for c in scope if c.type not in PPP_TYPES]
        if not non_ppp:
            continue  # PPP only: RADIUS already refuses the excess login live
        ppp_count = sum(counts.get(c.id, 0) for c in scope if c.type in PPP_TYPES)
        not_limited_online = [c for c in non_ppp if c.online and not c.session_limited]
        effective_count = ppp_count + len(not_limited_online)
        kicked = []
        if limit:
            excess = effective_count - limit
            for conn in sorted((c for c in not_limited_online if c.enabled), key=lambda c: c.id, reverse=True):
                if excess <= 0:
                    break
                kicked.append(conn)
                excess -= 1
        limited = [c for c in non_ppp if c.session_limited] + kicked
        if limited:
            room = len(limited) if not limit else max(limit - effective_count, 0)
            for conn in sorted(limited, key=lambda c: c.id):
                if room <= 0:
                    break
                governing_status = conn.purchase.status if conn.purchase_id and conn.purchase else user.status
                if governing_status != models.UserStatus.active:
                    continue  # quota/expiry still says no - leave it kicked, doesn't consume room
                releases.append((conn, limit, effective_count))
                room -= 1
        kicks.extend((conn, limit, effective_count) for conn in kicked)
    return kicks, releases
