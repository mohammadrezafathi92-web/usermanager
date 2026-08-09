import datetime as dt
import logging

import sentry_sdk
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect

from .config import settings
from .database import Base, engine, SessionLocal
from . import models
from .security import hash_password
from .services.quota_manager import poll_all
from .services.radius_server import start_radius_server_in_background, cleanup_stale_radius_sessions, cleanup_old_radius_limit_logs
from .services.notify import run_daily_notify_job
from .services.backup import run_scheduled_backup, ha_healthcheck, ha_pull_and_apply, notify_admins_text
from .routers import auth, nodes, users, dashboard, bot, api_keys, packages, panel_settings, telegram_bot_settings, tutorials, backup, remote_bot, admins, radius_logs, discount_codes, subscription, accounting as accounting_router
from .services import accounting as accounting_service
from .services import purchase_migration
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

    send_default_pii stays False on purpose: this panel's own data
    (MikroTik/SSH credentials, API keys, customer wallet balances, RADIUS
    secrets) is sensitive - Sentry gets exception type/traceback/request
    path, never request bodies/headers/user IP by default."""
    if not settings.sentry_dsn:
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
    )
    logging.info("Sentry error monitoring فعال شد (environment=%s)", settings.sentry_environment)


_init_sentry()

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(nodes.router)
app.include_router(users.router)
app.include_router(dashboard.router)
app.include_router(api_keys.router)
app.include_router(bot.router)
app.include_router(packages.router)
app.include_router(panel_settings.router)
app.include_router(telegram_bot_settings.router)
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

scheduler = BackgroundScheduler()

# HA (مورد ۱۰): consecutive failed peer health-checks/syncs, tracked
# in-process (not persisted - a process restart naturally resets this,
# which is fine since it just means "start counting from zero again").
_ha_consecutive_failures = 0
HA_FAILOVER_THRESHOLD = 5  # ~5 failures at the 20s tick interval below -> ~100s of the peer being unreachable before auto-promoting


_DEFAULT_SECRET_KEY = "change-this-secret-in-production"
_DEFAULT_ADMIN_PASSWORD = "admin123"


def _warn_if_insecure_defaults() -> None:
    """Loud (but non-fatal - this deliberately doesn't refuse to start, to
    avoid bricking an already-running deployment whose .env predates this
    check) startup warning when the JWT signing secret and/or the
    first-run admin password are still at their insecure hardcoded
    defaults. Anyone who knows the default SECRET_KEY can forge a valid
    admin session token; anyone who knows the default admin password can
    just log in."""
    if settings.secret_key == _DEFAULT_SECRET_KEY:
        logging.warning(
            "!!! هشدار امنیتی: SECRET_KEY تنظیم نشده و مقدار پیش‌فرض ناامن در حال استفاده است - "
            "یک مقدار تصادفی و طولانی در backend/.env با کلید SECRET_KEY تنظیم کنید و سرویس را ری‌استارت کنید !!!"
        )
    if settings.default_admin_password == _DEFAULT_ADMIN_PASSWORD:
        logging.warning(
            "!!! هشدار امنیتی: رمز عبور پیش‌فرض ادمین (admin123) هنوز در حال استفاده است - "
            "حتما از پنل وارد شوید و رمز عبور را تغییر دهید، یا DEFAULT_ADMIN_PASSWORD را در backend/.env قبل از اولین اجرا تنظیم کنید !!!"
        )
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


@app.on_event("startup")
def on_startup():
    _warn_if_insecure_defaults()
    _admin_node_access_is_new = "admin_node_access" not in set(inspect(engine).get_table_names())
    Base.metadata.create_all(bind=engine)
    _auto_migrate_missing_columns()
    _backfill_hierarchy_node_access(_admin_node_access_is_new)

    db = SessionLocal()
    try:
        if not db.query(models.AdminUser).first():
            admin = models.AdminUser(
                username=settings.default_admin_username,
                hashed_password=hash_password(settings.default_admin_password),
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

    # One-time conversion of legacy shared-pool services into independent
    # Purchases (see services/purchase_migration.py's docstring).
    db = SessionLocal()
    try:
        converted, skipped = purchase_migration.migrate_if_needed(db)
        if converted or skipped:
            logging.info("مهاجرت سرویس‌ها: %s تبدیل شد، %s اشتراکی ماند", converted, skipped)
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
    # Once a day - quota/expiry reminder messages via the sales bot
    # (best-effort no-op if the bot isn't running/configured).
    scheduler.add_job(run_daily_notify_job, "cron", hour=10, minute=0, id="daily_notify", replace_existing=True)
    # 4x/day full-database backup, sent to the bot's configured admins
    # (best-effort no-op if the bot isn't running/configured - see
    # services/backup.py). Also triggerable on-demand from Settings.
    scheduler.add_job(run_scheduled_backup, "cron", hour="0,6,12,18", minute=0, id="auto_backup", replace_existing=True)

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
        own_bot_admins = (
            db.query(models.AdminUser)
            .filter(models.AdminUser.own_bot_token.isnot(None), models.AdminUser.own_bot_enabled == True)  # noqa: E712
            .all()
        )
        for admin in own_bot_admins:
            telegram_bot_runner.start_admin_bot(admin.id, admin.own_bot_token, admin.telegram_id)
        if own_bot_admins:
            logging.info("started %d per-admin dedicated telegram bot(s)", len(own_bot_admins))
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
