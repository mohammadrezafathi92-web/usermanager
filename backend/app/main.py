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
from .services.backup import run_scheduled_backup
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

scheduler = BackgroundScheduler()


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

    if not scheduler.running:
        scheduler.add_job(poll_all, "interval", seconds=settings.poll_interval_seconds, id="poll_all", replace_existing=True)
        scheduler.add_job(cleanup_stale_radius_sessions, "interval", minutes=5, id="cleanup_stale_radius_sessions", replace_existing=True)
        # Once a day - quota/expiry reminder messages via the sales bot
        # (best-effort no-op if the bot isn't running/configured).
        scheduler.add_job(run_daily_notify_job, "cron", hour=10, minute=0, id="daily_notify", replace_existing=True)
        # 4x/day full-database backup, sent to the bot's configured admins
        # (best-effort no-op if the bot isn't running/configured - see
        # services/backup.py). Also triggerable on-demand from Settings.
        scheduler.add_job(run_scheduled_backup, "cron", hour="0,6,12,18", minute=0, id="auto_backup", replace_existing=True)
        scheduler.start()

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


@app.on_event("shutdown")
def on_shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    telegram_bot_runner.stop_bot(timeout=5)


@app.get("/api/health")
def health():
    return {"status": "ok"}
