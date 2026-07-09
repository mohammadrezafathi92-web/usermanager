"""Runtime configuration for the built-in Telegram bot.

Unlike the old standalone telegram-bot project this replaces, there is no
.env file here - the token and admin ids live in the BotSettings DB row
(managed from the panel's "ربات تلگرام" settings page) and get pushed into
this module's singleton via configure() whenever the panel starts up or the
admin saves the settings page. The only thing still read from the
environment is where to put the bot's own small sqlite file (pending
purchase requests), which defaults to the same /app/data volume the panel
itself already uses."""
import os


class RuntimeConfig:
    def __init__(self):
        self.bot_token: str = ""
        self.admin_ids: set[int] = set()
        self.approval_chat_ids: set[int] = set()
        self.db_path: str = os.environ.get("BOT_DB_PATH", "/app/data/bot_data.db")
        # "Maintenance mode" switch for the customer-facing side of the bot
        # (see routers/telegram_bot_settings.py, runner.py's
        # MaintenanceModeMiddleware) - True = normal operation. Admins are
        # never affected by this, only regular customers.
        self.customer_bot_enabled: bool = True

    def configure(
        self, bot_token: str, admin_ids: set[int], approval_chat_ids: set[int], customer_bot_enabled: bool = True,
    ) -> None:
        self.bot_token = bot_token or ""
        self.admin_ids = set(admin_ids or set())
        self.approval_chat_ids = set(approval_chat_ids or set())
        self.customer_bot_enabled = customer_bot_enabled

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    def approval_targets(self) -> set[int]:
        return self.approval_chat_ids or self.admin_ids


config = RuntimeConfig()


def parse_id_set(raw: str) -> set[int]:
    """Parses a comma-separated string of numeric Telegram ids (as typed
    into the settings page) into a set[int], silently skipping anything
    that isn't a valid integer."""
    ids: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids
