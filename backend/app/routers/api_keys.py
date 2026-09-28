from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require_superadmin, require_confirm_password
from ..services.bot_auth import (
    ALL_CAPABILITIES,
    DEFAULT_CAPABILITIES_BY_KEY_TYPE,
    KeyType,
    hash_api_key,
    parse_capabilities,
    serialize_capabilities,
)
from ..services.keys import generate_api_key

# Phase B of the API-key scope migration. Management stays superadmin-only;
# tenant keys record their future owner/capabilities but remain disabled and
# unenforced until Phase C wires one central BotPrincipal dependency across
# every /api/bot endpoint. Existing legacy keys keep their old behavior.
router = APIRouter(prefix="/api/api-keys", tags=["api-keys"], dependencies=[Depends(require_superadmin)])


def _capabilities_for(key: models.ApiKey) -> list[str]:
    parsed = parse_capabilities(key.capabilities)
    if parsed is None:
        parsed = DEFAULT_CAPABILITIES_BY_KEY_TYPE.get(key.key_type, frozenset())
    return sorted(parsed)


def _response(db: Session, key: models.ApiKey) -> schemas.ApiKeyOut:
    owner_username = None
    if key.owner_admin_id is not None:
        owner = db.get(models.AdminUser, key.owner_admin_id)
        owner_username = owner.username if owner else None
    return schemas.ApiKeyOut(
        id=key.id,
        label=key.label,
        key=key.key,
        enabled=bool(key.enabled),
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        owner_admin_id=key.owner_admin_id,
        owner_admin_username=owner_username,
        key_type=key.key_type,
        capabilities=_capabilities_for(key),
        scope_enforced=bool(key.scope_enforced),
        created_by_admin_id=key.created_by_admin_id,
    )


def _secret_fields(raw: str) -> dict:
    return {
        "key": raw,
        "key_hash": hash_api_key(raw),
        "key_prefix": raw[:8],
        "key_last4": raw[-4:] if len(raw) >= 4 else raw,
    }


@router.get("", response_model=list[schemas.ApiKeyOut])
def list_keys(db: Session = Depends(get_db)):
    rows = db.query(models.ApiKey).order_by(models.ApiKey.id.desc()).all()
    return [_response(db, row) for row in rows]


@router.post("", response_model=schemas.ApiKeyOut)
def create_key(
    payload: schemas.ApiKeyCreate,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(require_superadmin),
):
    """Create a tenant key in observation-only form.

    It is intentionally impossible to create or toggle this key enabled in
    Phase B. Its plaintext is still returned/stored for compatibility; that
    changes only in the separately reviewed Phase D.
    """
    owner = db.get(models.AdminUser, payload.owner_admin_id)
    if not owner or owner.is_superadmin:
        raise HTTPException(400, "مالک کلید باید یک ادمین یا فروشنده معتبر باشد")

    capabilities = (
        payload.capabilities
        if payload.capabilities is not None
        else sorted(DEFAULT_CAPABILITIES_BY_KEY_TYPE[KeyType.TENANT_INTEGRATION])
    )
    try:
        serialized = serialize_capabilities(capabilities)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    raw = generate_api_key()
    key = models.ApiKey(
        label=payload.label.strip(),
        owner_admin_id=owner.id,
        key_type=KeyType.TENANT_INTEGRATION,
        capabilities=serialized,
        scope_enforced=False,
        enabled=False,
        created_by_admin_id=current.id,
        **_secret_fields(raw),
    )
    if not key.label:
        raise HTTPException(400, "برچسب کلید نمی‌تواند خالی باشد")
    db.add(key)
    db.commit()
    db.refresh(key)
    return _response(db, key)


@router.post("/global", response_model=schemas.ApiKeyOut)
def create_global_key(
    payload: schemas.GlobalApiKeyCreate,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(require_confirm_password),
):
    """Create an explicitly global credential after password confirmation."""
    label = payload.label.strip()
    if not label:
        raise HTTPException(400, "برچسب کلید نمی‌تواند خالی باشد")
    raw = generate_api_key()
    key = models.ApiKey(
        label=label,
        owner_admin_id=None,
        key_type=KeyType.GLOBAL_INTEGRATION,
        capabilities=serialize_capabilities(ALL_CAPABILITIES),
        scope_enforced=False,
        enabled=True,
        created_by_admin_id=current.id,
        **_secret_fields(raw),
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return _response(db, key)


@router.post("/{key_id}/toggle", response_model=schemas.ApiKeyOut)
def toggle_key(key_id: int, db: Session = Depends(get_db)):
    key = db.get(models.ApiKey, key_id)
    if not key:
        raise HTTPException(404, "کلید پیدا نشد")
    if key.key_type == KeyType.TENANT_INTEGRATION and not key.enabled:
        raise HTTPException(
            409,
            "کلیدهای محدود به مالک تا تکمیل Phase C قابل فعال‌سازی نیستند",
        )
    # If a tenant key was manually/accidentally enabled in the database,
    # the same control remains able to disable it immediately.
    key.enabled = False if key.key_type == KeyType.TENANT_INTEGRATION else not key.enabled
    db.commit()
    db.refresh(key)
    return _response(db, key)


@router.post("/{key_id}/activate", response_model=schemas.ApiKeyOut)
def activate_tenant_key(
    key_id: int, db: Session = Depends(get_db), _confirmed: models.AdminUser = Depends(require_confirm_password),
):
    """Phase C (docs/api-key-scope-audit-2026-09-27.md) - the only way a
    tenant_integration key ever becomes usable. Sets enabled AND
    scope_enforced together, in one UPDATE with `enabled=False` as a
    precondition - never two separate writes, which could otherwise leave
    a moment where enabled=true but scope_enforced=false on the row (a
    state BotPrincipal.from_api_key treats as outright invalid, by design,
    rather than as an unscoped/legacy key - see its own docstring).
    toggle_key above still only ever moves an already-active tenant key
    back to disabled; this is the one path that turns one on."""
    result = db.execute(
        update(models.ApiKey.__table__)
        .where(
            models.ApiKey.id == key_id,
            models.ApiKey.key_type == KeyType.TENANT_INTEGRATION,
            models.ApiKey.enabled == False,  # noqa: E712
        )
        .values(enabled=True, scope_enforced=True)
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(409, "کلید در وضعیت قابل‌فعال‌سازی نیست")
    key = db.get(models.ApiKey, key_id)
    return _response(db, key)


@router.delete("/{key_id}")
def delete_key(key_id: int, db: Session = Depends(get_db), _confirm=Depends(require_confirm_password)):
    key = db.get(models.ApiKey, key_id)
    if not key:
        raise HTTPException(404, "کلید پیدا نشد")
    db.delete(key)
    db.commit()
    return {"ok": True}
