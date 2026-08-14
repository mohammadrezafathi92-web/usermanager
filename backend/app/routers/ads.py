"""The «تبلیغات» section's API - each admin manages their OWN channel and
their own rotation of posts (see services/ads.py).

Scoping is by ownership, not by role: every endpoint resolves the caller's
own AdChannel and only ever touches posts belonging to it. There is no
"see everyone's adverts" view even for a superadmin, for the same reason
their dashboard doesn't show other admins' customers - an Admin's channel,
audience and pricing are their own.
"""
import datetime as dt
import os
import shutil
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_admin, require_admin_or_above, require_confirm_password
from ..services import ads

router = APIRouter(prefix="/api/ads", tags=["ads"], dependencies=[Depends(require_admin_or_above)])

# Same volume as tutorial media (see routers/tutorials.py) - the /app/data
# bind mount, so uploads survive a container rebuild.
AD_MEDIA_DIR = os.environ.get("AD_MEDIA_DIR", "/app/data/ad_media")
# Telegram rejects photos over 10MB via the Bot API; anything larger could
# never be posted, so refuse it here where the admin gets a clear message.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


# ------------------------------------------------------------------ schemas
class ChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    chat_id: Optional[str] = None
    enabled: bool = False
    interval_hours: int = 6
    auto_send: bool = True
    delete_previous: bool = True
    last_sent_at: Optional[dt.datetime] = None
    last_error: Optional[str] = None
    sent_count: int = 0


class ChannelUpdate(BaseModel):
    chat_id: Optional[str] = None
    enabled: Optional[bool] = None
    interval_hours: Optional[int] = None
    auto_send: Optional[bool] = None
    delete_previous: Optional[bool] = None


class PostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: Optional[str] = None
    body: str = ""
    package_id: Optional[int] = None
    discount_code_id: Optional[int] = None
    image_name: Optional[str] = None
    button_text: Optional[str] = None
    enabled: bool = True
    sort_order: int = 0
    sent_count: int = 0
    last_sent_at: Optional[dt.datetime] = None


class PostIn(BaseModel):
    title: Optional[str] = None
    body: str = ""
    package_id: Optional[int] = None
    discount_code_id: Optional[int] = None
    button_text: Optional[str] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None


# ------------------------------------------------------------------ helpers
def _channel_for(db: Session, admin: models.AdminUser) -> models.AdChannel:
    """Created on first access rather than at admin-creation time, so this
    feature needs no backfill over existing admins."""
    row = db.query(models.AdChannel).filter(models.AdChannel.owner_admin_id == admin.id).first()
    if row is None:
        row = models.AdChannel(owner_admin_id=admin.id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _own_post(db: Session, admin: models.AdminUser, post_id: int) -> models.AdPost:
    channel = _channel_for(db, admin)
    post = db.get(models.AdPost, post_id)
    if post is None or post.channel_id != channel.id:
        raise HTTPException(404, "پست تبلیغاتی پیدا نشد")
    return post


# ------------------------------------------------------------------ channel
@router.get("/channel", response_model=ChannelOut)
def get_channel(db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    return ChannelOut.model_validate(_channel_for(db, admin))


@router.put("/channel", response_model=ChannelOut)
def update_channel(
    payload: ChannelUpdate,
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(get_current_admin),
):
    channel = _channel_for(db, admin)
    data = payload.model_dump(exclude_unset=True)
    if "interval_hours" in data and data["interval_hours"] is not None:
        # An interval below an hour would be spam and would also outpace the
        # 10-minute scheduler tick, making the setting meaningless.
        data["interval_hours"] = max(1, min(int(data["interval_hours"]), 24 * 14))
    if "chat_id" in data and data["chat_id"] is not None:
        data["chat_id"] = data["chat_id"].strip() or None
    for k, v in data.items():
        setattr(channel, k, v)
    # A settings change is an explicit "try again" - clear the stale failure
    # so the panel doesn't keep showing an error the admin just fixed.
    channel.last_error = None
    db.commit()
    db.refresh(channel)
    return ChannelOut.model_validate(channel)


@router.get("/placeholders")
def get_placeholders():
    return ads.PLACEHOLDERS


# -------------------------------------------------------------------- posts
@router.get("/posts", response_model=list[PostOut])
def list_posts(db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    channel = _channel_for(db, admin)
    rows = (
        db.query(models.AdPost)
        .filter(models.AdPost.channel_id == channel.id)
        .order_by(models.AdPost.sort_order, models.AdPost.id)
        .all()
    )
    return [PostOut.model_validate(r) for r in rows]


@router.post("/posts", response_model=PostOut)
def create_post(payload: PostIn, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    channel = _channel_for(db, admin)
    post = models.AdPost(channel_id=channel.id, **payload.model_dump(exclude_unset=True))
    db.add(post)
    db.commit()
    db.refresh(post)
    return PostOut.model_validate(post)


@router.put("/posts/{post_id}", response_model=PostOut)
def update_post(
    post_id: int, payload: PostIn,
    db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin),
):
    post = _own_post(db, admin, post_id)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(post, k, v)
    db.commit()
    db.refresh(post)
    return PostOut.model_validate(post)


@router.delete("/posts/{post_id}")
def delete_post(
    post_id: int,
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(get_current_admin),
    _confirm=Depends(require_confirm_password),
):
    post = _own_post(db, admin, post_id)
    if post.image_path:
        try:
            os.remove(post.image_path)
        except OSError:
            pass
    db.delete(post)
    db.commit()
    return {"ok": True}


@router.post("/posts/{post_id}/image", response_model=PostOut)
def upload_post_image(
    post_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(get_current_admin),
):
    post = _own_post(db, admin, post_id)
    os.makedirs(AD_MEDIA_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1].lower() or ".jpg"
    dest = os.path.join(AD_MEDIA_DIR, f"post{post.id}{ext}")

    size = 0
    with open(dest, "wb") as out:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                out.close()
                os.remove(dest)
                raise HTTPException(400, "حداکثر حجم تصویر ۱۰ مگابایت است")
            out.write(chunk)

    # Replacing an image with a different extension would otherwise leave the
    # old file behind forever.
    if post.image_path and post.image_path != dest:
        try:
            os.remove(post.image_path)
        except OSError:
            pass
    post.image_path = dest
    post.image_name = file.filename
    db.commit()
    db.refresh(post)
    return PostOut.model_validate(post)


@router.delete("/posts/{post_id}/image", response_model=PostOut)
def delete_post_image(
    post_id: int, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin),
):
    post = _own_post(db, admin, post_id)
    if post.image_path:
        try:
            os.remove(post.image_path)
        except OSError:
            pass
    post.image_path = None
    post.image_name = None
    db.commit()
    db.refresh(post)
    return PostOut.model_validate(post)


# ------------------------------------------------------------------ actions
@router.get("/posts/{post_id}/preview")
def preview_post(post_id: int, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    """Exactly what would be posted, placeholders filled with live data - so
    a mistake is caught in the panel rather than in front of the channel."""
    post = _own_post(db, admin, post_id)
    channel = _channel_for(db, admin)
    _token, bot_username = ads._bot_for(db, channel)
    return {"text": ads.render(post, bot_username), "has_image": bool(post.image_path)}


@router.post("/posts/{post_id}/send")
def send_now(post_id: int, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    post = _own_post(db, admin, post_id)
    channel = _channel_for(db, admin)
    ok, error = ads.send_post(db, channel, post, manual=True)
    db.commit()
    if not ok:
        raise HTTPException(400, error or "ارسال ناموفق بود")
    return {"ok": True}
