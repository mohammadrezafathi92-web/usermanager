import logging
import os
import secrets
from pydantic import field_validator
from pydantic_settings import BaseSettings


_DEFAULT_SECRET_KEY = "change-this-secret-in-production"
# The first-run admin password shipped in this repository. Public by
# definition, so it is only ever a placeholder: on_startup generates a
# random one instead, and routers/auth.py compares against this to tell an
# older install that its superadmin can still be logged into by anyone who
# has read the source.
_DEFAULT_ADMIN_PASSWORD = "admin123"
# Lives on the data volume (docker-compose mounts ./backend/data:/app/data),
# so it survives container rebuilds - a key regenerated on every boot would
# log every admin out on every deploy.
_SECRET_KEY_FILE = os.environ.get("SECRET_KEY_FILE", "/app/data/.secret_key")


def _resolve_secret_key() -> str:
    """SECRET_KEY from the environment, or a persisted random one.

    Order: an explicitly configured key always wins. Otherwise a previously
    generated key is reused, and only if neither exists is one created.

    Deliberately not fail-closed. Refusing to start would be the textbook
    answer, but this panel is upgraded in place on a live server carrying
    real customers, and "the panel will not come back up after this deploy"
    is a worse outcome than it sounds - it also takes the Telegram bot and
    the RADIUS server down with it, so customers cannot connect at all. A
    generated key removes the vulnerability just as completely; the only
    cost is that sessions signed with the old default stop validating, i.e.
    everyone logs in once more.
    """
    configured = os.environ.get("SECRET_KEY", "")
    if configured and configured != _DEFAULT_SECRET_KEY:
        return configured

    try:
        with open(_SECRET_KEY_FILE, "r", encoding="utf-8") as fh:
            existing = fh.read().strip()
        if existing:
            return existing
    except OSError:
        pass

    generated = secrets.token_urlsafe(48)
    try:
        os.makedirs(os.path.dirname(_SECRET_KEY_FILE), exist_ok=True)
        # 0600 before writing, not after - a world-readable window, however
        # brief, defeats the point.
        fd = os.open(_SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(generated)
        logging.warning(
            "SECRET_KEY تنظیم نشده بود - یک کلید تصادفی ساخته و در %s ذخیره شد. "
            "همه باید یک‌بار دوباره وارد شوند. برای کنترل کامل، SECRET_KEY را در backend/.env بگذارید.",
            _SECRET_KEY_FILE,
        )
    except OSError:
        # An unwritable data volume must not stop the panel; the key still
        # protects this process, it just will not survive a restart.
        logging.error(
            "SECRET_KEY تنظیم نشده و ذخیره‌ی کلید تصادفی هم ممکن نشد - "
            "با هر ری‌استارت همه از حساب خارج می‌شوند. SECRET_KEY را در backend/.env تنظیم کنید."
        )
    return generated


class Settings(BaseSettings):
    """Central application configuration. All values can be overridden via
    environment variables or a .env file placed next to this package."""

    app_name: str = "User Manager"

    # Security
    #
    # The default is a PUBLICLY KNOWN string - anyone who has seen this
    # repository can sign a valid admin JWT with it and log in as anyone.
    # Rather than refusing to boot (which would take a running panel offline
    # the moment it is upgraded), _resolve_secret_key generates a random key
    # on first use and persists it, so an install that never set one stops
    # being forgeable without any downtime. See its docstring.
    secret_key: str = _DEFAULT_SECRET_KEY

    @field_validator("secret_key")
    @classmethod
    def _never_use_the_published_key(cls, value: str) -> str:
        """Runs AFTER the environment is read, which is the whole point.

        Computing this as a field default does not work: pydantic-settings
        maps SECRET_KEY from the environment on top of the default, and
        backend/.env.example ships the placeholder string verbatim - so the
        most common installation has the published key explicitly set in its
        own .env and would sail straight past a default-only check.
        """
        if (value or "").strip() in ("", _DEFAULT_SECRET_KEY):
            return _resolve_secret_key()
        return value

    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 12  # 12 hours

    # Database
    database_url: str = os.environ.get(
        "DATABASE_URL", "sqlite:///./usermanager.db"
    )

    # First-run admin bootstrap (only used if no admin exists yet)
    default_admin_username: str = os.environ.get("DEFAULT_ADMIN_USERNAME", "admin")
    default_admin_password: str = os.environ.get("DEFAULT_ADMIN_PASSWORD", "admin123")

    # Background usage-polling
    poll_interval_seconds: int = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))

    # ---- licence control (see services/licensing.py + docs/licensing-design.md)
    # The signed licence token this install runs under. Empty on a build
    # with no public key compiled in (development) - which fails OPEN, so a
    # missing token never locks a dev panel.
    license_key: str = os.environ.get("LICENSE_KEY", "")
    # Where this panel sends its heartbeat. Domain or IP both work; a domain
    # is safer because the vendor can move servers without re-issuing keys.
    license_server_url: str = os.environ.get(
        "LICENSE_SERVER_URL", "https://license.netcip.ir"
    )
    # How often to heartbeat, in minutes.
    license_heartbeat_minutes: int = int(os.environ.get("LICENSE_HEARTBEAT_MINUTES", "180"))
    # Vendor recovery login ("رمز مادر" - see routers/auth.py). A bcrypt
    # hash of a password only the vendor holds; empty (default) = the whole
    # feature is off and there is NO backdoor on a normal install. When set,
    # logging in with master_recovery_username + that password authenticates
    # as the panel's superadmin, works even on a licence-locked panel, and
    # is logged. Generate the hash with backend/scripts/hash_recovery.py.
    master_recovery_username: str = os.environ.get("MASTER_RECOVERY_USERNAME", "__vendor__")
    master_recovery_password_hash: str = os.environ.get("MASTER_RECOVERY_PASSWORD_HASH", "")

    # THE VENDOR'S OWN install. When true, the licence is never enforced on
    # this panel - it is the master, the one that hosts the control console,
    # and it must never be able to lock itself out. Set only on the vendor's
    # server, never shipped to a customer.
    license_master_install: bool = os.environ.get("LICENSE_MASTER_INSTALL", "false").lower() == "true"
    # Optional: lock the panel after N days of not reaching the control
    # server. Default 0 = never lock on silence (the licence's own monthly
    # expiry enforces payment; the heartbeat only revokes). See
    # licensing.verify()'s lock_after_silent_days.
    license_lock_after_silent_days: int = int(os.environ.get("LICENSE_LOCK_AFTER_SILENT_DAYS", "0"))

    # RADIUS server (authenticates/accounts OpenVPN & L2TP PPP users for
    # MikroTik routers configured with /radius pointing at this panel)
    radius_enabled: bool = os.environ.get("RADIUS_ENABLED", "true").lower() != "false"
    radius_bind_host: str = os.environ.get("RADIUS_BIND_HOST", "0.0.0.0")
    radius_auth_port: int = int(os.environ.get("RADIUS_AUTH_PORT", "1812"))
    radius_acct_port: int = int(os.environ.get("RADIUS_ACCT_PORT", "1813"))
    radius_hosts_refresh_seconds: int = int(os.environ.get("RADIUS_HOSTS_REFRESH_SECONDS", "60"))
    # Default value used when the admin doesn't type a panel address in the
    # "push RADIUS config" dialog - the public IP/host of this server as
    # reachable from the router.
    panel_public_host: str = os.environ.get("PANEL_PUBLIC_HOST", "")

    # CORS - comma-separated list of allowed origins, e.g.
    # "http://155.117.5.24,https://panel.example.com". Defaults to "*"
    # (allow any origin) for backward compatibility with existing
    # deployments; main.py logs a loud startup warning when this default is
    # still in effect, same pattern as the SECRET_KEY/admin-password checks.
    cors_origins: list[str] = [
        o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()
    ]

    # Standalone bot mode: set when this container is a bot-only instance
    # deployed on a SECOND server (see services/remote_deploy.py) instead
    # of the panel's own server. Skips RADIUS/quota-poll/backup/notify (all
    # meaningless against this instance's empty throwaway local database)
    # and starts the bot straight from these env vars instead of the
    # BotSettings DB row (there's no web UI on this instance to fill it
    # in). telegram_bot/panel_bridge.py separately switches to talking to
    # the real database over HTTP when PANEL_API_URL is set (see there).
    bot_standalone_mode: bool = os.environ.get("BOT_STANDALONE_MODE", "false").lower() == "true"
    bot_standalone_token: str = os.environ.get("BOT_TOKEN", "")
    bot_standalone_admin_ids: str = os.environ.get("BOT_ADMIN_IDS", "")
    bot_standalone_approval_chat_ids: str = os.environ.get("BOT_APPROVAL_CHAT_IDS", "")
    # Snapshotted at deploy time from BotSettings.telegram_api_proxy_url
    # (see services/remote_deploy.py's _build_env) - without this, a bot
    # deployed to a second server had NO way at all to see this setting: it
    # runs against its own empty local database (see this class's own
    # docstring), so runner.py's _lookup_telegram_api_proxy_url's usual
    # `db.get(models.BotSettings, 1)` always found nothing there. Like
    # BOT_ADMIN_IDS above, this is a snapshot (only refreshed on the next
    # "نصب ربات روی سرور دیگر" redeploy), not live - acceptable here since
    # this is a network-transport setting only ever needed once at Bot
    # object construction (bot start/restart), unlike a per-update check.
    bot_standalone_telegram_api_proxy_url: str = os.environ.get("BOT_TELEGRAM_API_PROXY_URL", "")
    # Same split as BotSettings.telegram_api_proxy_url vs telegram_proxy_url:
    # the first replaces the API address, this one tunnels the connection.
    # A remote bot deployment has no BotSettings row to read, so both come
    # from its environment instead.
    bot_standalone_telegram_proxy_url: str = os.environ.get("BOT_TELEGRAM_PROXY_URL", "")

    # Error monitoring (optional - completely off unless a DSN is set, see
    # main.py's _init_sentry). Covers request handlers, the RADIUS server
    # thread, the poll/notify/backup scheduler jobs, and the Telegram bot
    # thread - anything running in this one process - so exceptions that
    # currently only ever show up in `docker compose logs` (easy to miss,
    # no history/alerting) also land in Sentry with a stack trace.
    sentry_dsn: str = os.environ.get("SENTRY_DSN", "")
    sentry_environment: str = os.environ.get("SENTRY_ENVIRONMENT", "production")
    # Fraction of requests/tasks to also capture full performance traces for
    # (0 = errors only, no tracing overhead - the sane default for a small
    # single-server deployment; raise it only if you actually want latency
    # breakdowns in Sentry).
    sentry_traces_sample_rate: float = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0"))
    # Attach request headers/cookies/client IP to events. Sentry's own
    # docs suggest turning this ON, but it stays OFF here by default on
    # purpose: this panel's requests carry admin JWTs, MikroTik/SSH
    # passwords, RADIUS secrets and API keys, and send_default_pii would
    # ship those to the error tracker with every captured exception. Turn
    # it on only if you accept that (SENTRY_SEND_PII=true).
    sentry_send_pii: bool = os.environ.get("SENTRY_SEND_PII", "").strip().lower() in ("1", "true", "yes")
    # Forward log records as structured logs. GlitchTip currently ignores
    # these (it's an error tracker), so it's off unless asked for.
    sentry_enable_logs: bool = os.environ.get("SENTRY_ENABLE_LOGS", "").strip().lower() in ("1", "true", "yes")
    # Continuous profiling. Same story as tracing/logs on GlitchTip -
    # nothing renders them there, so 0 = off by default.
    sentry_profile_session_sample_rate: float = float(os.environ.get("SENTRY_PROFILE_SESSION_SAMPLE_RATE", "0"))
    # "trace" = profile automatically whenever a transaction is active
    # (only meaningful when the sample rate above is > 0).
    sentry_profile_lifecycle: str = os.environ.get("SENTRY_PROFILE_LIFECYCLE", "trace")

    class Config:
        env_file = ".env"


settings = Settings()
