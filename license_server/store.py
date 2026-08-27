"""The licence server's data + decisions - the part with no HTTP in it.

This is a SEPARATE service from the panel (its own container, port, DB and
login), running only on the vendor's own server. Customer panels never run
it; they only call its /heartbeat. So it shares no models, no auth and no
database with the panel - deliberately. If the panel is compromised, this
is not; and the vendor's private control data never sits in a customer's
reach.

Everything an operator does - register an install, revoke it, choose how
hard a revoked install locks - happens here as plain functions over a DB
session, so the whole thing is testable without standing up a web server.
"""
from __future__ import annotations

import datetime as dt
import secrets
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Integer, String, Text, create_engine, func,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

Base = declarative_base()

# Mirror of the panel's licensing.LOCK_SCOPES. Kept as bare strings rather
# than an import, because this service must build and run with NONE of the
# panel's code present (it ships on its own). The two lists are checked
# against each other in the tests instead.
SCOPE_PANEL_ONLY = "panel_only"
SCOPE_PANEL_AND_BOT = "panel_and_bot"
SCOPE_EVERYTHING = "everything"
LOCK_SCOPES = (SCOPE_PANEL_ONLY, SCOPE_PANEL_AND_BOT, SCOPE_EVERYTHING)
DEFAULT_LOCK_SCOPE = SCOPE_PANEL_ONLY


class Install(Base):
    """One deployed panel, as the licence server sees it.

    A row appears the first time an install sends a heartbeat (it
    self-registers), and is updated on every heartbeat after. The operator
    then names it, decides whether it is allowed, and how hard to lock it if
    not.
    """

    __tablename__ = "installs"

    id = Column(Integer, primary_key=True)

    # Identity as claimed by the install. license_id comes from the signed
    # licence; fingerprint is the hardware id. Together they say "which
    # install is this" - and a mismatch between heartbeats (same license_id,
    # new fingerprint) is exactly the "someone copied it to a second server"
    # signal the operator wants to see.
    license_id = Column(String(64), unique=True, index=True, nullable=False)
    fingerprint = Column(String(64), nullable=True)

    # What the operator calls this customer. Free text, theirs to set.
    label = Column(String(200), nullable=True)

    # The lever. revoked = the heartbeat tells the install to lock; scope =
    # how hard (mirrors the panel's own resolve_enforcement). Off by default:
    # a freshly-seen install is allowed until the operator says otherwise.
    revoked = Column(Boolean, default=False, nullable=False)
    lock_scope = Column(String(20), default=DEFAULT_LOCK_SCOPE, nullable=False)

    # Observed facts, updated every heartbeat - what the operator reads to
    # decide anything.
    first_seen = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    last_seen = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    last_ip = Column(String(64), nullable=True)
    fingerprint_changed_at = Column(DateTime, nullable=True)
    heartbeat_count = Column(Integer, default=0, nullable=False)

    # The customer-facing version/panel details the heartbeat reports, so
    # the operator can see who is on an old build without asking.
    panel_version = Column(String(64), nullable=True)
    reported_customers = Column(Integer, nullable=True)

    note = Column(Text, nullable=True)


def make_engine(url: str):
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        # An in-memory DB lives inside ONE connection. Under the web server
        # every request/thread would otherwise open its own empty database
        # and see "no such table". StaticPool keeps a single shared
        # connection so the schema (and the data) is the same for everyone -
        # correct for :memory: in tests, and harmless for a real file.
        if ":memory:" in url or url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(url: str):
    return sessionmaker(bind=make_engine(url), autoflush=False, future=True)


# --------------------------------------------------------------------------
# what a heartbeat does
# --------------------------------------------------------------------------
def record_heartbeat(
    db,
    *,
    license_id: str,
    fingerprint: Optional[str] = None,
    ip: Optional[str] = None,
    panel_version: Optional[str] = None,
    reported_customers: Optional[int] = None,
    now: Optional[dt.datetime] = None,
) -> tuple[Install, dict]:
    """Register-or-update an install, and return what to tell it.

    Returns (install_row, response_dict). The response is the ONLY thing the
    install acts on, so it is deliberately tiny and explicit:

        {"revoked": bool, "lock_scope": str}

    A brand-new install self-registers as allowed. That is on purpose: the
    common case is the vendor bringing a paying customer online, and making
    them wait for a manual approval before their panel works would turn a
    sale into a support ticket. Payment is enforced by the licence's expiry,
    not by this switch - this switch is only for taking someone OFF.
    """
    now = now or dt.datetime.utcnow()
    if not license_id:
        raise ValueError("license_id is required")

    install = db.query(Install).filter(Install.license_id == license_id).one_or_none()
    if install is None:
        install = Install(
            license_id=license_id,
            fingerprint=fingerprint,
            first_seen=now,
            last_seen=now,
            last_ip=ip,
            panel_version=panel_version,
            reported_customers=reported_customers,
            heartbeat_count=1,
        )
        db.add(install)
    else:
        # A fingerprint that changes on an existing licence is the copy
        # signal - stamped so the operator sees WHEN, not just that it
        # differs now. The new value is stored (the install genuinely moved,
        # or the hardware genuinely changed); the operator decides whether
        # that was legitimate.
        if fingerprint and install.fingerprint and fingerprint != install.fingerprint:
            install.fingerprint_changed_at = now
        if fingerprint:
            install.fingerprint = fingerprint
        install.last_seen = now
        install.last_ip = ip or install.last_ip
        install.panel_version = panel_version or install.panel_version
        if reported_customers is not None:
            install.reported_customers = reported_customers
        install.heartbeat_count = (install.heartbeat_count or 0) + 1

    db.commit()
    return install, {
        "revoked": bool(install.revoked),
        "lock_scope": install.lock_scope or DEFAULT_LOCK_SCOPE,
    }


# --------------------------------------------------------------------------
# operator actions
# --------------------------------------------------------------------------
def list_installs(db) -> list[Install]:
    """Most recently active first - the operator wants to see who is live."""
    return db.query(Install).order_by(Install.last_seen.desc()).all()


def get_install(db, license_id: str) -> Optional[Install]:
    return db.query(Install).filter(Install.license_id == license_id).one_or_none()


def set_revoked(db, license_id: str, revoked: bool) -> Optional[Install]:
    install = get_install(db, license_id)
    if install is None:
        return None
    install.revoked = bool(revoked)
    db.commit()
    return install


def set_lock_scope(db, license_id: str, scope: str) -> Optional[Install]:
    if scope not in LOCK_SCOPES:
        raise ValueError(f"unknown lock scope: {scope!r}")
    install = get_install(db, license_id)
    if install is None:
        return None
    install.lock_scope = scope
    db.commit()
    return install


def set_label(db, license_id: str, label: str, note: Optional[str] = None) -> Optional[Install]:
    install = get_install(db, license_id)
    if install is None:
        return None
    install.label = label
    if note is not None:
        install.note = note
    db.commit()
    return install


def forget_install(db, license_id: str) -> bool:
    """Remove an install the operator no longer wants listed. It will
    re-register if it heartbeats again - so this is 'clear the row', not
    'block forever'; blocking is what revoked is for."""
    install = get_install(db, license_id)
    if install is None:
        return False
    db.delete(install)
    db.commit()
    return True


# --------------------------------------------------------------------------
# operator authentication - a single password, hashed, for the console
# --------------------------------------------------------------------------
class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)


def _get_setting(db, key: str) -> Optional[str]:
    row = db.get(Setting, key)
    return row.value if row else None


def _set_setting(db, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def _hash_password(password: str, salt: Optional[str] = None) -> str:
    import hashlib
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"pbkdf2${salt}${digest.hex()}"


def verify_password(stored: str, password: str) -> bool:
    try:
        _, salt, _ = stored.split("$")
    except (ValueError, AttributeError):
        return False
    return secrets.compare_digest(stored, _hash_password(password, salt))


def ensure_admin_password(db, default_password: str) -> str:
    """Sets the console password on first run and returns whatever it is.

    Only the vendor ever sees this service, but it still gets a real login
    so a shared server or a curious neighbour on the box can't just open the
    revoke switches. The password lives only as a hash.
    """
    existing = _get_setting(db, "admin_password_hash")
    if existing:
        return "(existing)"
    _set_setting(db, "admin_password_hash", _hash_password(default_password))
    return default_password


def check_admin_password(db, password: str) -> bool:
    stored = _get_setting(db, "admin_password_hash")
    if not stored:
        return False
    return verify_password(stored, password)


def set_admin_password(db, new_password: str) -> None:
    _set_setting(db, "admin_password_hash", _hash_password(new_password))
