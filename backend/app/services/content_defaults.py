"""Where «ایمپورت پیش‌فرض‌ها» gets its content from.

The button used to copy the texts written into app/default_content.py, and
that was the wrong source. Those texts were written blind, by someone who
has never sold this product - while the panel owner has spent months
writing tutorials that name the actual apps, and adverts in their own
voice. Feedback 2026-09-12: "پکیج‌ها و آموزش‌های من خیلی بهتر از این
دیفالتایی که ساختی" - and a reseller pressing that button is asking to be
handed the house content, not a stranger's placeholder for it.

So the panel owner's own list is the source whenever they have one, and the
shipped texts are what is left for the case nothing else exists: a fresh
install, where the owner's list is empty too. They are the floor, not the
default.

Two deliberate asymmetries, both inherited from what the two lists mean:

  * Tutorials copy only the ones the owner has switched ON. `enabled` on a
    tutorial means "customers see this", so an off one is something the
    owner has taken out of circulation and has no business being pushed
    down the tree, switched on, into someone else's bot.
  * Adverts copy regardless, and arrive OFF, exactly as before. `enabled`
    on an advert is "include me in MY rotation", which says nothing about
    whether it is worth handing on - and an advert must never start
    broadcasting to a channel because a button was pressed.

Nothing here reads a reseller's own parent. A level-2 Admin's curated list
does not become their Sellers' source; everyone below copies from the panel
owner. That is what was asked for, and it keeps the answer to "where did
this text come from" a single place rather than a walk up the tree.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .. import models
from ..default_content import DEFAULT_ADS, DEFAULT_TUTORIALS

# Returned to the panel so it can say WHERE the content came from - "۷ مورد
# اضافه شد" is much less reassuring than knowing it was the house list.
SOURCE_PANEL_OWNER = "panel_owner"
SOURCE_BUILTIN = "builtin"


def tutorials_source(db: Session, admin: models.AdminUser) -> tuple[list[dict], str]:
    """(items, source) - items are {"title", "text"} dicts, in order."""
    if not admin.is_superadmin:
        # The superadmin's own tutorials are the rows with no owner at all
        # (see routers/tutorials.py's owner_id convention).
        rows = (
            db.query(models.Tutorial)
            .filter(models.Tutorial.owner_admin_id.is_(None), models.Tutorial.enabled.is_(True))
            .order_by(models.Tutorial.sort_order, models.Tutorial.id)
            .all()
        )
        if rows:
            return [{"title": r.title or "", "text": r.text or ""} for r in rows], SOURCE_PANEL_OWNER
    return [dict(d) for d in DEFAULT_TUTORIALS], SOURCE_BUILTIN


def ads_source(db: Session, admin: models.AdminUser) -> tuple[list[dict], str]:
    """(items, source) - items are {"title", "body"} dicts, in order."""
    if not admin.is_superadmin:
        owner = (
            db.query(models.AdminUser)
            .filter(models.AdminUser.is_superadmin.is_(True))
            .order_by(models.AdminUser.id)
            .first()
        )
        # Unlike tutorials, an ad channel is keyed by the superadmin's own
        # id - _channel_for in routers/ads.py makes one per admin row,
        # including theirs.
        channel = (
            db.query(models.AdChannel).filter(models.AdChannel.owner_admin_id == owner.id).first()
            if owner is not None else None
        )
        if channel is not None:
            rows = (
                db.query(models.AdPost)
                .filter(models.AdPost.channel_id == channel.id)
                .order_by(models.AdPost.sort_order, models.AdPost.id)
                .all()
            )
            if rows:
                return [{"title": r.title or "", "body": r.body or ""} for r in rows], SOURCE_PANEL_OWNER
    return [dict(d) for d in DEFAULT_ADS], SOURCE_BUILTIN
