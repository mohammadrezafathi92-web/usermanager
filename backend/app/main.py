import datetime as dt
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import Base, engine, SessionLocal
from . import models
from .security import hash_password
from .services.quota_manager import poll_all
from .services.radius_server import start_radius_server_in_background, cleanup_stale_radius_sessions
from .services.notify import run_daily_notify_job
from .services.backup import run_scheduled_backup, ha_healthcheck, ha_pull_and_apply, notify_admins_text
from .routers import auth, nodes, users, dashboard, bot, api_keys, packages, panel_settings, telegram_bot_settings, tutorials, backup, remote_bot, admins
from .telegram_bot import runner as telegram_bot_runner
from .telegram_bot.config import parse_id_set

logging.basicConfig(level=logging.INFO)

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
app.include_router(remote_bot.router)
app.include_router(admins.router)
app.include_router(panel_settings.ha_router)

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


@app.on_event("startup")
def on_startup():
    _warn_if_insecure_defaults()
    Base.metadata.create_all(bind=engine)

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
    old primary's data)."""
    global _ha_consecutive_failures
    db = SessionLocal()
    try:
        row = db.get(models.PanelSettings, 1)
        if not row or not row.ha_enabled or row.ha_mode != "standby" or row.ha_standby_active:
            return
        if not row.ha_peer_url or not row.ha_peer_api_key:
            return
        try:
            healthy, reason = ha_healthcheck(row.ha_peer_url)
            if not healthy:
                raise RuntimeError(f"بررسی سلامت سرور اصلی ناموفق بود: {reason}")
            ha_pull_and_apply(row.ha_peer_url, row.ha_peer_api_key)
            row.ha_last_sync_at = dt.datetime.utcnow()
            row.ha_last_health_ok_at = dt.datetime.utcnow()
            row.ha_last_error = None
            db.commit()
            _ha_consecutive_failures = 0
        except Exception as exc:
            _ha_consecutive_failures += 1
            # Prefix with a UTC timestamp so the admin can tell a stale
            # error (left over from before a config fix) from a fresh one
            # without cross-referencing server logs - this field has no
            # separate "last attempt" timestamp of its own, only
            # ha_last_sync_at/ha_last_health_ok_at which only update on
            # SUCCESS, so a repeatedly-failing standby previously showed the
            # exact same untimed error message forever.
            now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            row.ha_last_error = f"[{now}] ({_ha_consecutive_failures}/{HA_FAILOVER_THRESHOLD}) {exc}"
            db.commit()
            logging.warning(
                "HA: بررسی/همگام‌سازی سرور اصلی ناموفق بود (%s/%s): %s",
                _ha_consecutive_failures,
                HA_FAILOVER_THRESHOLD,
                exc,
            )
            if _ha_consecutive_failures >= HA_FAILOVER_THRESHOLD:
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
