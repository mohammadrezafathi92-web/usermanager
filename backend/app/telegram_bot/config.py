"""Runtime configuration for the built-in Telegram bot.

Unlike the old standalone telegram-bot project this replaces, there is no
.env file here - the token and admin ids live in the BotSettings DB row
(managed from the panel's "ربات تلگرام" settings page) and get pushed into
this module's singleton via configure() whenever the panel starts up or the
admin saves the settings page. The only thing still read from the
environment is where to put the bot's own small sqlite file (pending
purchase requests), which defaults to the same /app/data volume the panel
itself already uses.

Multi-bot note (3-tier hierarchy feature - see telegram_bot/runner.py's
registry): every bot instance (the one shared/global bot AND any number of
per-admin bots, each on its own dedicated background thread) reads/writes
`config` through this exact same `from .config import config; config.xxx`
pattern used across every handler file - nothing in those files needed to
change. What makes that safe is that RuntimeConfig subclasses
threading.local: each thread gets its OWN independent copy of every
attribute, so runner.py's _main() calling config.configure(...) at the top
of one bot's thread can never bleed into another bot's thread reading the
same `config` name at the same time."""
import os
import threading


class RuntimeConfig(threading.local):
    def __init__(self):
        super().__init__()
        self.bot_token: str = ""
        self.admin_ids: set[int] = set()
        self.approval_chat_ids: set[int] = set()
        self.db_path: str = os.environ.get("BOT_DB_PATH", "/app/data/bot_data.db")
        # "Maintenance mode" switch for the customer-facing side of the bot
        # (see routers/telegram_bot_settings.py, runner.py's
        # MaintenanceModeMiddleware) - True = normal operation. Admins are
        # never affected by this, only regular customers.
        self.customer_bot_enabled: bool = True
        # None = this is the shared/global bot (unscoped, sees/creates
        # customers panel-wide, exactly the pre-hierarchy behavior). Set to
        # a level-2 Admin's id for that Admin's OWN dedicated bot (see
        # AdminUser.own_bot_token) - every new customer created through
        # THIS bot automatically belongs to that Admin's tree, and every
        # lookup is scoped to it too. See panel_bridge.py's _scope() helper,
        # the one place this actually gets applied.
        self.bot_owner_admin_id: int | None = None

    def configure(
        self, bot_token: str, admin_ids: set[int], approval_chat_ids: set[int], customer_bot_enabled: bool = True,
        bot_owner_admin_id: int | None = None,
    ) -> None:
        self.bot_token = bot_token or ""
        self.admin_ids = set(admin_ids or set())
        self.approval_chat_ids = set(approval_chat_ids or set())
        self.customer_bot_enabled = customer_bot_enabled
        self.bot_owner_admin_id = bot_owner_admin_id

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
