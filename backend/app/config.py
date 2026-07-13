import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central application configuration. All values can be overridden via
    environment variables or a .env file placed next to this package."""

    app_name: str = "User Manager"

    # Security
    secret_key: str = os.environ.get("SECRET_KEY", "change-this-secret-in-production")
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

    class Config:
        env_file = ".env"


settings = Settings()
