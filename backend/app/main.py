import datetime as dt
import logging
import os
import secrets

import sentry_sdk
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect

from .config import settings
from .database import Base, engine, SessionLocal
from . import models
from .security import hash_password
from .services.quota_manager import poll_all, cleanup_old_usage_logs
from .services import jalali
from .services.ads import run_due_campaigns
from .services.radius_server import start_radius_server_in_background, cleanup_stale_radius_sessions, cleanup_old_radius_limit_logs
from .services.notify import run_daily_notify_job
from .services.backup import run_scheduled_backup, ha_healthcheck, ha_pull_and_apply, notify_admins_text
from .routers import auth, nodes, users, dashboard, bot, api_keys, packages, panel_settings, telegram_bot_settings, telegram_proxy, tg_tunnel, tutorials, backup, remote_bot, admins, radius_logs, discount_codes, subscription, accounting as accounting_router, ads as ads_router, license as license_router, ip_bans as ip_bans_router
from .services import accounting as accounting_service
from .services import purchase_migration
from .services import ip_guard
from .telegram_bot import runner as telegram_bot_runner
from .telegram_bot.config import parse_id_set

logging.basicConfig(level=logging.INFO)


def _init_sentry() -> None:
    """Opt-in error monitoring - a complete no-op if SENTRY_DSN isn't set
    (the default), so this changes nothing for anyone who doesn't want it.
    Deliberately called before the FastAPI app and any background
    thread (RADIUS server, scheduler, Telegram bot) is created, since
    sentry_sdk.init() installs its exception hooks process-wide - anything
    started after this point (including new threads) is automatically
    covered, not just request handlers.

    Every knob Sentry's own FastAPI quickstart shows is wired up here, but
    driven by env vars (see config.py) rather than hardcoded, because the
    quickstart's defaults are written for sentry.io and this deployment
    points at a self-hosted GlitchTip:

    - send_default_pii defaults to FALSE, unlike the quickstart. This
      panel's requests carry admin JWTs, MikroTik/SSH passwords, RADIUS
      secrets and API keys; turning it on ships those to the error tracker
      with every captured exception. SENTRY_SEND_PII=true opts in.
    - enable_logs / profiling default to off: GlitchTip is an error
      tracker and renders neither, so sending them is pure extra traffic.
      Both become available the moment the DSN points at something that
      does support them.

    The FastAPI/Starlette integrations are NOT passed explicitly - the SDK
    auto-enables them when fastapi is installed (confirmed in the received
    events' `integrations` list), and naming them by hand only risks
    breaking on an SDK upgrade that moves them."""
    if not settings.sentry_dsn:
        return

    options = {
        "dsn": settings.sentry_dsn,
        "environment": settings.sentry_environment,
        "traces_sample_rate": settings.sentry_traces_sample_rate,
        "send_default_pii": settings.sentry_send_pii,
        "enable_logs": settings.sentry_enable_logs,
    }
    if settings.sentry_profile_session_sample_rate > 0:
        options["profile_session_sample_rate"] = settings.sentry_profile_session_sample_rate
        options["profile_lifecycle"] = settings.sentry_profile_lifecycle

    try:
        sentry_sdk.init(**options)
    except TypeError:
        # An older sentry-sdk that doesn't know one of the newer options -
        # fall back to the core set rather than leaving monitoring off
        # entirely (the errors matter more than the extras).
        logging.warning("این نسخه sentry-sdk همه گزینه‌ها را پشتیبانی نمی‌کند - با تنظیمات پایه فعال شد")
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.sentry_environment,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            send_default_pii=settings.sentry_send_pii,
        )

    logging.info(
        "ردیابی خطا فعال شد (environment=%s, pii=%s, logs=%s, traces=%s)",
        settings.sentry_environment, settings.sentry_send_pii,
        settings.sentry_enable_logs, settings.sentry_traces_sample_rate,
    )


_init_sentry()

# Interactive API docs are OFF unless asked for.
#
# They are a development convenience that shipped to production by default:
# /docs and /openapi.json describe every endpoint, every field name and the
# bot API's X-API-Key header, to anyone who can reach the port. The
# endpoints themselves are authenticated, so this is disclosure rather than
# direct access - but it is a free map of the attack surface, handed out
# with no login.
#
# Kept behind a switch rather than deleted so debugging a live install is
# still one env var away.
_docs_enabled = os.environ.get("ENABLE_API_DOCS", "").strip().lower() in ("1", "true", "yes")

app = FastAPI(
    title=settings.app_name,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def ip_guard_middleware(request: Request, call_next):
    """Global abuse guard - runs before every single request, ahead of any
    router or auth dependency, so a banned IP is refused outright rather
    than merely failing the endpoint's own auth check. The actual logic
    lives in services/ip_guard.guard_request (kept testable there without
    booting this whole app)."""
    return await ip_guard.guard_request(request, call_next, SessionLocal)


app.include_router(auth.router)
app.include_router(nodes.router)
app.include_router(users.router)
app.include_router(dashboard.router)
app.include_router(api_keys.router)
app.include_router(bot.router)
app.include_router(packages.router)
app.include_router(panel_settings.router)
app.include_router(telegram_bot_settings.router)
app.include_router(telegram_proxy.router)
app.include_router(tg_tunnel.router)
app.include_router(tutorials.router)
app.include_router(backup.router)
app.include_router(backup.my_router)
app.include_router(remote_bot.router)
app.include_router(admins.router)
app.include_router(radius_logs.router)
app.include_router(discount_codes.router)
app.include_router(panel_settings.my_payment_router)
app.include_router(panel_settings.ha_router)
app.include_router(subscription.router)
app.include_router(accounting_router.router)
app.include_router(ads_router.router)
app.include_router(license_router.router)
app.include_router(ip_bans_router.router)

scheduler = BackgroundScheduler()

# HA (مورد ۱۰): consecutive failed peer health-checks/syncs, tracked
# in-process (not persisted - a process restart naturally resets this,
# which is fine since it just means "start counting from zero again").
_ha_consecutive_failures = 0
HA_FAILOVER_THRESHOLD = 5  # ~5 failures at the 20s tick interval below -> ~100s of the peer being unreachable before auto-promoting


_DEFAULT_SECRET_KEY = "change-this-secret-in-production"
# Imported rather than redefined - see config._DEFAULT_ADMIN_PASSWORD.
from .config import _DEFAULT_ADMIN_PASSWORD  # noqa: E402


def _warn_if_insecure_defaults() -> None:
    """Loud (but non-fatal - this deliberately doesn't refuse to start, to
    avoid bricking an already-running deployment whose .env predates this
    check) startup warning when the JWT signing secret and/or the
    first-run admin password are still at their insecure hardcoded
    defaults. Anyone who knows the default SECRET_KEY can forge a valid
    admin session token; anyone who knows the default admin password can
    just log in."""
    # config._resolve_secret_key now replaces the published default with a
    # generated, persisted key, so `secret_key == _DEFAULT_SECRET_KEY` can
    # never be true here any more and checking it would be a warning that
    # never fires. What is still worth saying is that the key is not under
    # the operator's control - it lives in a file rather than their .env,
    # so it is absent from their backups and from a rebuilt server.
    if not (os.environ.get("SECRET_KEY", "") or "").strip():
        logging.warning(
            "SECRET_KEY در backend/.env تنظیم نشده - یک کلید تصادفی روی دیسک استفاده می‌شود. "
            "اگر سرور را از نو بسازید یا فایل داده را از دست بدهید، همه از حساب خارج می‌شوند. "
            "برای کنترل کامل، SECRET_KEY را در backend/.env بگذارید."
        )
    # The generated first-run password (see on_startup) means a fresh
    # install is no longer born with a known password. This warning is for
    # the case that matters now: an install created BEFORE that change,
    # whose superadmin may still be sitting on admin123.
    if settings.default_admin_password == _DEFAULT_ADMIN_PASSWORD:
        db = SessionLocal()
        try:
            from .security import verify_password

            weak = [
                a.username for a in db.query(models.AdminUser).filter(models.AdminUser.is_superadmin == True).all()  # noqa: E712
                if verify_password(_DEFAULT_ADMIN_PASSWORD, a.hashed_password)
            ]
            if weak:
                logging.warning(
                    "!!! هشدار امنیتی: این حساب(های) سوپرادمین هنوز رمز عبور «admin123» دارند: %s - "
                    "همین حالا از پنل عوضشان کنید !!!",
                    "، ".join(weak),
                )
        except Exception:
            logging.debug("could not check for the default admin password", exc_info=True)
        finally:
            db.close()
    if settings.cors_origins == ["*"]:
        logging.warning(
            "!!! هشدار امنیتی: CORS برای همه دامنه‌ها باز است (*) - "
            "برای محدود کردن، متغیر CORS_ORIGINS را در backend/.env با آدرس(های) واقعی پنل (مثلا http://IP-سرور یا https://دامنه) تنظیم کنید و سرویس را ری‌استارت کنید !!!"
        )


def _auto_migrate_missing_columns() -> None:
    """Adds any columns/indexes that exist in the ORM models (models.py)
    but not yet in the actual database file, so a fresh `git pull` +
    rebuild can never crash the app on a stale schema again.

    Until now this project relied on the admin manually running a
    migrate_*.sql (or migrate_*.py) file by hand after certain deploys -
    easy to forget, and exactly what happened on 2026-07-14: a server
    that had been updated to the new code crashed on every startup with
    "no such column: panel_settings.support_contact_text" because nobody
    had run that day's .sql file on it yet. This makes that whole class of
    bug impossible: every startup compares the ORM's expected columns
    against what's actually in the database and adds whatever's missing,
    automatically, before the app starts serving requests.

    Deliberately ADDITIVE ONLY - never drops/renames/alters an existing
    column, never touches existing data. Runs after
    Base.metadata.create_all() (which handles brand-new TABLES on its
    own) and only fills in columns/indexes on tables that already exist.
    Any single column/index that fails to add is logged and skipped
    rather than aborting startup - a missing index is not worth crashing
    over, and a column ALTER failure will surface clearly the first time
    something actually queries that column, same as before this existed.

    Also runs against MySQL/MariaDB (added when the "choose your database
    at install time" feature landed) - `ADD COLUMN ... DEFAULT ...` is
    valid DDL on both, and column.type.compile(dialect=...) already
    produces the correct dialect-specific type string, so the only
    dialect-specific wrinkle handled below is that MySQL/MariaDB reject a
    literal DEFAULT on TEXT/BLOB/JSON columns (see is_lob_type)."""
    if engine.dialect.name not in ("sqlite", "mysql", "mariadb"):
        # Unknown/unsupported dialect - skip rather than risk an incorrect
        # ALTER on something nobody has tested this against.
        return

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # brand-new table - create_all() above already made it

            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_cols:
                    continue
                try:
                    col_type = column.type.compile(dialect=engine.dialect)
                except Exception:
                    col_type = "TEXT"

                default_sql = ""
                default = getattr(column, "default", None)
                # MySQL/MariaDB reject a literal DEFAULT on TEXT/BLOB/JSON
                # columns ("BLOB, TEXT, GEOMETRY or JSON column can't have a
                # default value") - skip the DEFAULT clause for those so the
                # ALTER still succeeds; the column is just added NULL-default
                # instead, same as any other nullable column would be.
                is_lob_type = engine.dialect.name in ("mysql", "mariadb") and any(
                    kw in col_type.upper() for kw in ("TEXT", "BLOB", "JSON")
                )
                if default is not None and getattr(default, "is_scalar", False) and not is_lob_type:
                    arg = default.arg
                    if isinstance(arg, bool):
                        default_sql = f" DEFAULT {1 if arg else 0}"
                    elif isinstance(arg, (int, float)):
                        default_sql = f" DEFAULT {arg}"
                    elif isinstance(arg, str):
                        default_sql = f" DEFAULT '{arg}'"

                ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}{default_sql}"
                try:
                    conn.exec_driver_sql(ddl)
                    logging.info("auto-migrate: added missing column %s.%s", table.name, column.name)
                except Exception as exc:
                    logging.warning(
                        "auto-migrate: could not add column %s.%s (%s) - skipping",
                        table.name, column.name, exc,
                    )

            for index in table.indexes:
                try:
                    index.create(bind=conn, checkfirst=True)
                except Exception as exc:
                    logging.warning("auto-migrate: could not create index %s (%s) - skipping", index.name, exc)


def _backfill_hierarchy_node_access(admin_node_access_table_is_new: bool) -> None:
    """One-time backfill for the 3-tier reseller hierarchy feature (see
    services/hierarchy.py): before this feature existed, EVERY logged-in
    admin (superadmin or not) could see EVERY node - node visibility was
    never scoped. Now routers/nodes.py's list_nodes/_get_scoped_node
    restrict a non-superadmin ("level-2 Admin", since parent_admin_id also
    defaults NULL for every admin row that existed before this feature) to
    only nodes explicitly granted via AdminNodeAccess. Without this
    backfill, EVERY existing non-superadmin admin would silently lose
    access to every node the moment this update is deployed - a real
    production outage for whoever's already running the panel, not just a
    theoretical gap.

    Only runs the ONE time admin_node_access is first created (detected via
    admin_node_access_table_is_new, checked in on_startup BEFORE
    create_all() runs) - on every later startup the table already exists,
    so this is skipped entirely and whatever grants/revocations a
    superadmin has since made through the UI are left alone."""
    if not admin_node_access_table_is_new:
        return
    db = SessionLocal()
    try:
        existing_admin_ids = [
            row.id for row in db.query(models.AdminUser.id).filter(models.AdminUser.is_superadmin == False).all()  # noqa: E712
        ]
        existing_node_ids = [row.id for row in db.query(models.Node.id).all()]
        if not existing_admin_ids or not existing_node_ids:
            return
        for admin_id in existing_admin_ids:
            for node_id in existing_node_ids:
                db.add(models.AdminNodeAccess(admin_id=admin_id, node_id=node_id))
        db.commit()
        logging.info(
            "hierarchy backfill: granted %d pre-existing admin(s) access to all %d pre-existing node(s)",
            len(existing_admin_ids), len(existing_node_ids),
        )
    finally:
        db.close()


def _seed_default_permission_groups() -> None:
    """One-time seed of a couple of ready-made AdminPermissionGroup presets
    (task #26's "چنتا گروه پیش فرض هم بزار" - add a few default groups) so
    a superadmin creating their first level-3 Seller has something to pick
    from immediately instead of an empty list. Only ever grantable thing
    left in permissions.PERMISSION_CHOICES after this session's audit is
    view_tutorials (see permissions.py's module docstring for the full
    reasoning - every other former checkbox either did nothing for a
    Seller or was a real cross-tenant risk, so it was removed rather than
    kept as a confusing no-op checkbox) - so the only meaningful thing two
    presets can differ on today is that one flag. Gated on "no groups
    exist yet at all" (like _backfill_hierarchy_node_access's is-new
    check) so this only ever runs once and never re-creates/overwrites a
    group a superadmin has since renamed, edited, or deleted."""
    db = SessionLocal()
    try:
        if db.query(models.AdminPermissionGroup).first():
            return  # already seeded (or the superadmin made their own) - never touch it again
        db.add(models.AdminPermissionGroup(name="فروشنده استاندارد", permissions="view_tutorials"))
        db.add(models.AdminPermissionGroup(name="فروشنده محدود (بدون آموزش)", permissions=""))
        db.commit()
        logging.info("seeded 2 default AdminPermissionGroup presets")
    finally:
        db.close()


def _backfill_roles_and_paths() -> None:
    """Seeds AdminUser.role/tree_path/depth for rows created before those
    columns existed.

    Deliberately writes the role that the OLD derivation would have
    produced, not the role anyone thinks the account should have. This is a
    data-shape migration, not a policy change: an account that behaves as a
    Seller today must still behave as a Seller after restart, or an admin
    would find their permissions silently altered by a deploy. Correcting
    a genuinely mis-classified account is a separate, deliberate action in
    the panel.

    Idempotent - only rows with a NULL role or NULL path are touched, so
    every subsequent startup is a no-op and an operator's later correction
    is never overwritten.
    """
    from .services import hierarchy

    db = SessionLocal()
    try:
        admins = db.query(models.AdminUser).all()
        if not admins:
            return
        by_id = {a.id: a for a in admins}

        role_filled = 0
        for a in admins:
            if not (a.role or "").strip():
                a.role = hierarchy.derive_role(a)
                role_filled += 1

        # Paths are built parents-first: a child's path is its parent's
        # path plus its own id, so computing them in id order would embed
        # a not-yet-written parent path. Sorting by ancestor count is the
        # cheap way to guarantee the parent is always done first.
        def ancestor_count(a: models.AdminUser) -> int:
            n, seen, cur = 0, {a.id}, a
            while cur.parent_admin_id and cur.parent_admin_id in by_id:
                cur = by_id[cur.parent_admin_id]
                if cur.id in seen:
                    logging.error(
                        "hierarchy backfill: چرخه در درخت ادمین‌ها از %s - مسیر این شاخه ساخته نشد", a.username
                    )
                    return n
                seen.add(cur.id)
                n += 1
            return n

        path_filled = 0
        for a in sorted(admins, key=ancestor_count):
            if a.tree_path:
                continue
            parent = by_id.get(a.parent_admin_id) if a.parent_admin_id else None
            a.tree_path = hierarchy.build_path(parent.tree_path if parent else None, a.id)
            a.depth = hierarchy.path_depth(a.tree_path)
            path_filled += 1

        if role_filled or path_filled:
            db.commit()
            logging.info(
                "hierarchy backfill: نقش برای %d حساب و مسیر درخت برای %d حساب نوشته شد",
                role_filled, path_filled,
            )
    except Exception:
        db.rollback()
        # A failed backfill must not stop the panel from booting: the
        # fallback in hierarchy.role() keeps every existing account
        # working exactly as before, so this is degraded, not broken.
        logging.exception("hierarchy backfill ناموفق بود - رفتار قبلی دست‌نخورده باقی می‌ماند")
    finally:
        db.close()



def _grandfather_permissions() -> None:
    """Grants every newly-introduced permission to accounts that predate it.

    The capabilities these permissions gate - deleting customers, bulk
    operations, export, spending credit, accounting, discount codes, own bot
    - were all completely ungated before. Shipping the gates without this
    would take working features away from every existing Seller on the next
    deploy, and it would look to them like a settings change nobody made.

    So the deploy adds a capability (the ability to WITHHOLD these) rather
    than applying a restriction retroactively. The superadmin then unticks
    what they actually want to withhold.

    Runs once, guarded by PanelSettings.permissions_grandfathered. Only
    grants; never removes anything already stored.
    """
    from .permissions import PERMISSION_CHOICES, format_permissions, parse_permissions

    db = SessionLocal()
    try:
        # id=1 singleton, same convention as routers/panel_settings.py's
        # _get_or_create - .first() would happily create a second row on a
        # database that somehow had none at id 1.
        settings_row = db.get(models.PanelSettings, 1)
        if settings_row is None:
            settings_row = models.PanelSettings(id=1)
            db.add(settings_row)
            db.flush()
        if settings_row.permissions_grandfathered:
            return

        everything = set(PERMISSION_CHOICES)
        touched = 0

        # Groups first: an account IN a group reads its permissions from the
        # group (see permissions.effective_permissions), so granting only on
        # the account would leave grouped Sellers restricted anyway.
        for group in db.query(models.AdminPermissionGroup).all():
            merged = parse_permissions(group.permissions) | everything
            group.permissions = format_permissions(merged)
            touched += 1

        for admin in db.query(models.AdminUser).filter(models.AdminUser.is_superadmin.is_(False)).all():
            merged = parse_permissions(admin.permissions) | everything
            admin.permissions = format_permissions(merged)
            touched += 1

        settings_row.permissions_grandfathered = True
        db.commit()
        logging.info(
            "permissions: %d حساب/گروه موجود همه‌ی مجوزهای جدید را گرفتند تا رفتارشان عوض نشود", touched
        )
    except Exception:
        db.rollback()
        # Deliberately NOT fatal, but this one is worth shouting about: if
        # it fails, existing Sellers really will have lost abilities, and
        # the superadmin needs to know to re-tick them by hand.
        logging.exception(
            "permissions grandfathering ناموفق بود - ممکن است فروشنده‌های فعلی بعضی دسترسی‌ها را از دست بدهند"
        )
    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    _warn_if_insecure_defaults()
    _admin_node_access_is_new = "admin_node_access" not in set(inspect(engine).get_table_names())
    Base.metadata.create_all(bind=engine)
    _auto_migrate_missing_columns()
    _backfill_hierarchy_node_access(_admin_node_access_is_new)
    _backfill_roles_and_paths()

    # ip_guard's ban list is enforced from an in-memory set (checked on
    # every single request - a DB hit per request would be needless
    # overhead) - it has to be primed from the persisted table before the
    # first request is served, or a redeploy would briefly un-ban everyone.
    _ipb_db = SessionLocal()
    try:
        ip_guard.refresh_from_db(_ipb_db)
    finally:
        _ipb_db.close()
    # Must run BEFORE the first request is served: the moment the app is up,
    # a Seller hitting a newly-gated endpoint would get a 403 they never had.
    _grandfather_permissions()

    # The container is recreated on every deploy and takes the WireGuard
    # interface with it, so the tunnel is rebuilt from the database here.
    # Never fatal - see services/wg_tunnel.ensure_up.
    try:
        from .services import wg_tunnel
        _tdb = SessionLocal()
        try:
            wg_tunnel.ensure_up(_tdb.get(models.TelegramTunnel, 1))
        finally:
            _tdb.close()
    except Exception:
        logging.exception("بالا آوردن دوباره‌ی تونل تلگرام ناموفق بود")

    db = SessionLocal()
    try:
        if not db.query(models.AdminUser).first():
            # A fresh install with no DEFAULT_ADMIN_PASSWORD set used to get
            # the hardcoded "admin123" - published in this repository, so
            # anyone who found the panel could simply log in. A generated
            # password is printed ONCE here instead: it exists nowhere else,
            # so the operator must read it out of the first-boot log, and
            # there is no known value to try.
            initial_password = (settings.default_admin_password or "").strip()
            # Empty counts as "generate one", not as "an empty password".
            # .env.example now ships DEFAULT_ADMIN_PASSWORD= with no value,
            # and os.environ.get returns "" for that - not the fallback - so
            # without this check a fresh install would create a superadmin
            # whose password is the empty string.
            generated = initial_password in ("", _DEFAULT_ADMIN_PASSWORD)
            if generated:
                initial_password = secrets.token_urlsafe(12)
            admin = models.AdminUser(
                username=settings.default_admin_username,
                hashed_password=hash_password(initial_password),
                is_superadmin=True,
            )
            db.add(admin)
            db.commit()
            # Deliberately NOT logging the actual password value - it either
            # came from DEFAULT_ADMIN_PASSWORD (the admin who set it already
            # knows it) or is the insecure hardcoded default (already
            # flagged loudly by _warn_if_insecure_defaults above); echoing
            # secrets into logs is itself a leak vector (log files, log
            # aggregators, `docker compose logs` output shared for support).
            if generated:
                # The one place a secret is deliberately logged. It is the
                # only copy that exists, it is shown once on a brand-new
                # install with no data in it yet, and the alternative is a
                # password everybody already knows.
                logging.warning(
                    "\n" + "=" * 62
                    + "\n  ادمین اولیه ساخته شد"
                    + "\n  نام کاربری: %s"
                    + "\n  رمز عبور  : %s"
                    + "\n  این رمز فقط همین یک‌بار نمایش داده می‌شود - همین حالا"
                    + "\n  یادداشتش کنید و بعد از ورود عوضش کنید."
                    + "\n" + "=" * 62,
                    settings.default_admin_username, initial_password,
                )
            else:
                logging.info(
                    "ادمین پیش‌فرض ساخته شد -> username=%s (رمز عبور از DEFAULT_ADMIN_PASSWORD خوانده شد - حتما بعد از ورود تغییرش دهید)",
                    settings.default_admin_username,
                )
        elif not db.query(models.AdminUser).filter(models.AdminUser.is_superadmin == True).first():  # noqa: E712
            # Upgrade path: admin(s) already existed before is_superadmin
            # was introduced, so they all default to False (see the column
            # default) - without this, nobody could log in with full access
            # after updating. Auto-promote the oldest admin account once;
            # from then on this branch never fires again since a
            # superadmin will always exist.
            oldest = db.query(models.AdminUser).order_by(models.AdminUser.id).first()
            oldest.is_superadmin = True
            db.commit()
            logging.info("ادمین موجود «%s» به‌صورت خودکار ادمین اصلی (superadmin) شد", oldest.username)
    finally:
        db.close()

    _seed_default_permission_groups()

    # One-time import of pre-existing purchases/credit logs into the
    # accounting ledger (see services/accounting.py's backfill_if_needed -
    # guarded by PanelSettings.accounting_backfilled, so a no-op on every
    # startup after the first).
    db = SessionLocal()
    try:
        imported = accounting_service.backfill_if_needed(db)
        if imported:
            logging.info("حساب‌داری: %s رکورد تاریخی به دفتر کل منتقل شد", imported)
    except Exception:
        logging.exception("خطا در انتقال تاریخچه مالی به دفتر کل حساب‌داری")
    finally:
        db.close()

    # One-time labelling of existing sales-bot signups (see
    # models.User.created_via) - a customer with a linked telegram id and
    # no owning reseller can only have come from the bot's own signup flow,
    # since every panel-created user is stamped with its creator's admin id.
    db = SessionLocal()
    try:
        marked = (
            db.query(models.User)
            .filter(
                models.User.created_via.is_(None),
                models.User.telegram_id.isnot(None),
                models.User.owner_admin_id.is_(None),
            )
            .update({models.User.created_via: "bot"}, synchronize_session=False)
        )
        if marked:
            db.commit()
            logging.info("%s کاربر قدیمی به‌عنوان «ثبت‌نام از ربات» علامت خورد", marked)
    except Exception:
        logging.exception("خطا در علامت‌گذاری کاربران ثبت‌نام‌شده از ربات")
    finally:
        db.close()

    # One-time move of the old single Package.ovpn_template column into the
    # multi-file PackageOvpnTemplate table (see that model's docstring) -
    # the column is cleared afterwards, so this can't run twice.
    db = SessionLocal()
    try:
        legacy_pkgs = db.query(models.Package).filter(models.Package.ovpn_template.isnot(None)).all()
        for pkg in legacy_pkgs:
            if (pkg.ovpn_template or "").strip():
                db.add(models.PackageOvpnTemplate(
                    package_id=pkg.id, name="config.ovpn", content=pkg.ovpn_template, sort_order=0,
                ))
            pkg.ovpn_template = None
        if legacy_pkgs:
            db.commit()
            logging.info("انتقال %s فایل ovpn قدیمی پکیج‌ها به جدول جدید", len(legacy_pkgs))
    except Exception:
        logging.exception("خطا در انتقال فایل ovpn قدیمی پکیج‌ها")
    finally:
        db.close()

    # Prime the display timezone before anything can render a date. Cheap,
    # and it has to happen before the scheduler's first notify run.
    db = SessionLocal()
    try:
        row = db.query(models.PanelSettings).first()
        if row is not None:
            jalali.set_display_offset(row.display_utc_offset_minutes)
        logging.info("منطقه‌زمانی نمایش: UTC%+d:%02d", jalali.get_display_offset() // 60, abs(jalali.get_display_offset()) % 60)
    except Exception:
        logging.exception("خطا در خواندن منطقه‌زمانی نمایش - مقدار پیش‌فرض تهران استفاده می‌شود")
    finally:
        db.close()

    # One-time conversion of legacy shared-pool services into independent
    # Purchases (see services/purchase_migration.py's docstring).
    db = SessionLocal()
    try:
        converted, skipped = purchase_migration.migrate_if_needed(db)
        if converted or skipped:
            logging.info("مهاجرت سرویس‌ها: %s تبدیل شد، %s اشتراکی ماند", converted, skipped)
        # Repairs anyone left with BOTH a Purchase and shared-pool
        # connections - see fix_mixed_users' docstring for why that state
        # silently strands the leftover connections.
        purchase_migration.fix_mixed_users(db)
    except Exception:
        logging.exception("خطا در مهاجرت سرویس‌های قدیمی به Purchase مستقل")
    finally:
        db.close()

    if settings.bot_standalone_mode:
        # This container is a bot-only instance deployed on a second server
        # (see services/remote_deploy.py) - its local database is just an
        # empty throwaway file, so none of the panel's usual background
        # jobs (quota polling, RADIUS, daily notify, DB backup) make sense
        # here. Just start the bot straight from env vars; panel_bridge.py
        # points it at the real database over HTTP via PANEL_API_URL.
        logging.info("در حال اجرا در حالت «فقط ربات» (نصب روی سرور دوم) - RADIUS/زمان‌بند غیرفعال است")
        telegram_bot_runner.start_bot(
            settings.bot_standalone_token,
            parse_id_set(settings.bot_standalone_admin_ids),
            parse_id_set(settings.bot_standalone_approval_chat_ids),
        )
        return

    # HA (مورد ۱۰): the lightweight scheduler + ha_tick job always start
    # regardless of role, so a passive standby can keep polling/pulling
    # from its peer and auto-promote itself if needed - see ha_tick()
    # below. The HEAVY jobs (quota polling, RADIUS, bot) are what
    # _start_full_services() adds; a passive standby (ha enabled, this
    # server's role is "standby", and it hasn't already auto-promoted)
    # deliberately skips them at startup until ha_tick's _promote_to_active
    # starts them live on failover - for anyone not using HA (ha_enabled
    # stays False by default) this is 100% today's behavior, unchanged.
    if not scheduler.running:
        scheduler.add_job(ha_tick, "interval", seconds=20, id="ha_tick", replace_existing=True)
        scheduler.start()

    db = SessionLocal()
    try:
        panel_row = db.get(models.PanelSettings, 1)
        passive_standby = bool(
            panel_row and panel_row.ha_enabled and panel_row.ha_mode == "standby" and not panel_row.ha_standby_active
        )
    finally:
        db.close()

    if passive_standby:
        logging.info(
            "HA: در حال اجرا در حالت «آماده‌به‌کار» (standby) - تا فعال‌سازی (خودکار با فیل‌اور یا دستی)، "
            "RADIUS/زمان‌بند اصلی/ربات روی این سرور اجرا نمی‌شوند"
        )
        return

    _start_full_services()


def _start_full_services() -> None:
    """Starts the heavy quota/notify/backup scheduler jobs, the RADIUS
    server, and the interactive Telegram bot (if configured) - what a
    normal (non-HA, or HA primary/promoted-standby) instance always runs.
    Called from on_startup() directly, and again later by
    _promote_to_active() when a passive standby auto-fails-over live
    without a process restart."""
    scheduler.add_job(poll_all, "interval", seconds=settings.poll_interval_seconds, id="poll_all", replace_existing=True)
    scheduler.add_job(cleanup_stale_radius_sessions, "interval", minutes=5, id="cleanup_stale_radius_sessions", replace_existing=True)
    # Once a day - prune RadiusLimitEventLog rows older than
    # RADIUS_LIMIT_EVENT_LOG_KEEP_DAYS (7 by default - "لاگ‌های محدودیت
    # اتصال فقط یک هفته کافیه"). This table had no cleanup at all before -
    # every rejected/banned RADIUS auth attempt accumulated forever.
    scheduler.add_job(cleanup_old_radius_limit_logs, "cron", hour=3, minute=30, id="cleanup_old_radius_limit_logs", replace_existing=True)
    # UsageLog is written on every poll cycle and never pruned before this -
    # see services/quota_manager.py's cleanup_old_usage_logs docstring for
    # the unbounded-DB-growth problem it fixes.
    scheduler.add_job(cleanup_old_usage_logs, "cron", hour=3, minute=45, id="cleanup_old_usage_logs", replace_existing=True)
    # Once a day - quota/expiry reminder messages via the sales bot
    # (best-effort no-op if the bot isn't running/configured).
    scheduler.add_job(run_daily_notify_job, "cron", hour=10, minute=0, id="daily_notify", replace_existing=True)
    # 4x/day full-database backup, sent to the bot's configured admins
    # (best-effort no-op if the bot isn't running/configured - see
    # services/backup.py). Also triggerable on-demand from Settings.
    scheduler.add_job(run_scheduled_backup, "cron", hour="0,6,12,18", minute=0, id="auto_backup", replace_existing=True)
    # Channel adverts (see services/ads.py). One frequent tick that asks
    # each channel whether ITS interval has elapsed, rather than a
    # scheduler job per channel - channels are created and reconfigured
    # from the panel at runtime, and re-registering jobs on every
    # settings change is a synchronisation problem with nothing to gain.
    scheduler.add_job(run_due_campaigns, "interval", minutes=10, id="ad_campaigns", replace_existing=True)

    # Licence heartbeat to the vendor's control server (see
    # services/license_state.py). Best-effort and out of band - the panel
    # runs on its cached verdict between beats, and a failed beat changes
    # nothing. Skipped entirely on the vendor's own master install.
    from .services import license_state
    license_state.refresh()  # prime the cache before the first request
    if not settings.license_master_install:
        scheduler.add_job(
            license_state.heartbeat_job, "interval",
            minutes=max(5, settings.license_heartbeat_minutes),
            id="license_heartbeat", replace_existing=True,
            next_run_time=dt.datetime.now() + dt.timedelta(seconds=30),
        )

    start_radius_server_in_background()

    db = SessionLocal()
    try:
        bot_row = db.get(models.BotSettings, 1)
        if bot_row and bot_row.enabled and bot_row.bot_token:
            telegram_bot_runner.start_bot(
                bot_row.bot_token,
                parse_id_set(bot_row.admin_ids or ""),
                parse_id_set(bot_row.approval_chat_ids or ""),
                bot_row.customer_bot_enabled if bot_row.customer_bot_enabled is not None else True,
            )

        # Per-admin dedicated bots (3-tier hierarchy - see
        # AdminUser.own_bot_token/own_bot_enabled and telegram_bot/runner.py's
        # multi-instance registry) - every level-2 Admin who's configured
        # and enabled their own bot gets it started here too, running
        # concurrently with the shared bot above and every other admin's.
        #
        # Deliberately de-duplicated by token: Telegram allows exactly ONE
        # getUpdates poller per bot token, so starting the same token twice
        # doesn't give you two bots - the two instances kick each other off
        # in a loop and NEITHER receives messages. That really happened here
        # (2026-08-12): one token was saved on three different admin rows,
        # the panel started three pollers, and that admin's bot silently
        # received nothing for weeks while flooding the logs with
        # TelegramConflictError several times a second. routers/
        # telegram_bot_settings.py now rejects a duplicate token at save
        # time; this is the belt-and-braces half for rows that predate that
        # check (or get there via direct DB edits).
        own_bot_admins = (
            db.query(models.AdminUser)
            .filter(models.AdminUser.own_bot_token.isnot(None), models.AdminUser.own_bot_enabled == True)  # noqa: E712
            .order_by(models.AdminUser.id)  # lowest id wins - it's the original owner
            .all()
        )
        seen_tokens: set[str] = set()
        if bot_row and bot_row.enabled and bot_row.bot_token:
            seen_tokens.add(bot_row.bot_token.strip())
        started = 0
        for admin in own_bot_admins:
            token = (admin.own_bot_token or "").strip()
            if token in seen_tokens:
                logging.warning(
                    "ربات اختصاصی ادمین #%s استارت نشد: توکن %s قبلاً برای یک ربات دیگر در حال اجراست "
                    "(تلگرام فقط یک نمونه به‌ازای هر توکن را می‌پذیرد)",
                    admin.id, token.split(":")[0] or "?",
                )
                continue
            seen_tokens.add(token)
            telegram_bot_runner.start_admin_bot(admin.id, token, admin.telegram_id)
            started += 1
        if started:
            logging.info("started %d per-admin dedicated telegram bot(s)", started)
    finally:
        db.close()


def ha_tick() -> None:
    """APScheduler job (every 20s, started unconditionally in on_startup):
    if this server is an HA-enabled, not-yet-promoted standby, health-checks
    the peer and pulls its latest DB snapshot. No-ops instantly (cheap) for
    everyone else - HA disabled, this server is "primary", or it already
    auto-promoted itself (ha_standby_active - the split-brain guard: once
    True, this function returns immediately at the top forever, so a standby
    that took over never goes back to silently overwriting itself with the
    old primary's data).

    IMPORTANT (bug found 2026-07-13 via live testing - "دیتا سینک شد ولی
    وضعیت آپدیت نشد" / data replicated but the status fields never showed
    it): a successful ha_pull_and_apply() call does os.replace() on the live
    db file AND disposes the SQLAlchemy engine's connection pool. But the
    `db` Session opened at the top of this function had ALREADY checked out
    a connection (from the very first db.get() below) before that happens -
    and that specific connection keeps reading/writing the OLD, now-unlinked
    file via its still-open file descriptor (POSIX rename/replace doesn't
    invalidate fds already open on the old inode) until it's closed, engine
    dispose() only recycles the pool for FUTURE checkouts. So writing
    ha_last_sync_at/ha_last_error through that same original `db`/`row`
    right after a successful pull silently lands in the orphaned pre-swap
    file, which is discarded the instant this function's `db.close()` runs -
    the write appears to succeed (no exception, nothing in the logs) but is
    never actually visible again. Fix: do the health-check/pull using one
    short-lived session for just the read, then ALWAYS record the
    success/failure status through a brand-new session opened AFTER the
    pull - guaranteed to get a fresh connection against whatever file is
    actually live on disk at that point."""
    global _ha_consecutive_failures

    db = SessionLocal()
    try:
        row = db.get(models.PanelSettings, 1)
        if not row or not row.ha_enabled or row.ha_mode != "standby" or row.ha_standby_active:
            return
        if not row.ha_peer_url or not row.ha_peer_api_key:
            return
        peer_url = row.ha_peer_url
        peer_api_key = row.ha_peer_api_key
    finally:
        db.close()

    error_text = None
    try:
        healthy, reason = ha_healthcheck(peer_url)
        if not healthy:
            raise RuntimeError(f"بررسی سلامت سرور اصلی ناموفق بود: {reason}")
        ha_pull_and_apply(peer_url, peer_api_key)
    except Exception as exc:
        _ha_consecutive_failures += 1
        # Prefix with a UTC timestamp + attempt counter so the admin can
        # tell a stale error (left over from before a config fix) from a
        # fresh one without cross-referencing server logs.
        now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        error_text = f"[{now}] ({_ha_consecutive_failures}/{HA_FAILOVER_THRESHOLD}) {exc}"
        logging.warning(
            "HA: بررسی/همگام‌سازی سرور اصلی ناموفق بود (%s/%s): %s",
            _ha_consecutive_failures,
            HA_FAILOVER_THRESHOLD,
            exc,
        )
    else:
        _ha_consecutive_failures = 0

    # Always through a FRESH session opened here - see docstring above for
    # why reusing the session from the read at the top would silently
    # write into an orphaned file after a successful pull.
    db = SessionLocal()
    try:
        row = db.get(models.PanelSettings, 1)
        if not row:
            return
        if error_text is None:
            row.ha_last_sync_at = dt.datetime.utcnow()
            row.ha_last_health_ok_at = dt.datetime.utcnow()
            row.ha_last_error = None
        else:
            row.ha_last_error = error_text
        db.commit()
        need_promote = error_text is not None and _ha_consecutive_failures >= HA_FAILOVER_THRESHOLD and not row.ha_standby_active
        if need_promote:
            _promote_to_active(db, row)
    finally:
        db.close()


def _promote_to_active(db, row: "models.PanelSettings") -> None:
    """Auto-failover: called once ha_tick() has seen HA_FAILOVER_THRESHOLD
    consecutive failed health-checks/syncs against the primary (~100s of
    the peer being unreachable at the 20s tick interval). Flips this
    standby into an actively-serving instance - starts the same background
    jobs/RADIUS/bot on_startup would've started for a primary - and, the
    split-brain guard, permanently stops pulling snapshots from the peer
    from this point on (ha_tick short-circuits at the top once
    ha_standby_active is True) until an admin manually calls
    /api/ha/resolve after checking both servers by hand. Blindly resuming
    sync automatically once the old primary comes back could silently
    overwrite this server's own post-promotion writes with stale data from
    a peer that's back up but behind - that risk is exactly why this stays
    a manual step rather than something this function ever undoes on its
    own.

    No automatic DNS/floating-IP traffic switch happens here - per how
    this feature was configured, only a Telegram alert is sent; a human
    still needs to manually point users/the bot at this server."""
    row.ha_standby_active = True
    row.ha_promoted_at = dt.datetime.utcnow()
    db.commit()
    logging.warning("HA: فیل‌اور خودکار انجام شد - این سرور اکنون به‌صورت فعال سرویس می‌دهد")

    _start_full_services()

    try:
        sent, total = notify_admins_text(
            "⚠️ فیل‌اور خودکار HA انجام شد.\n\n"
            "این سرور به مدت حدود ۱۰۰ ثانیه نتوانست سرور اصلی را بررسی/همگام‌سازی کند و "
            "به‌صورت خودکار جای آن را گرفت و اکنون به‌طور کامل سرویس می‌دهد "
            "(RADIUS/زمان‌بند/ربات روی همین سرور روشن شد).\n\n"
            "⚠️ هیچ سوئیچ خودکاری روی DNS/IP انجام نشده - لازم است به‌صورت دستی ترافیک "
            "کاربران را به این سرور هدایت کنید.\n\n"
            "بعد از بررسی وضعیت هر دو سرور، از پنل > تنظیمات > HA گزینه «تایید و بازنشانی» را بزنید."
        )
        logging.info("HA: اعلان فیل‌اور به %s/%s ادمین تلگرام ارسال شد", sent, total)
    except Exception:
        logging.exception("HA: ارسال اعلان فیل‌اور به تلگرام ناموفق بود")


@app.on_event("shutdown")
def on_shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    telegram_bot_runner.stop_bot(timeout=5)


@app.get("/api/health")
def health():
    return {"status": "ok"}
