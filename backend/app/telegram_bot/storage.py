"""Tiny local sqlite store for pending purchase/renewal requests (the
card-to-card payment needs an admin to look at a receipt photo and click
Approve/Reject - this survives bot restarts between the customer sending
the receipt and an admin acting on it). Lives in the same /app/data volume
the panel's own database already uses, so it survives container
rebuilds/restarts just like everything else."""
import datetime as dt
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Optional

from .config import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    telegram_username TEXT,
    telegram_name TEXT,
    kind TEXT NOT NULL,              -- 'new' or 'renew'
    package_id INTEGER NOT NULL,
    package_name TEXT NOT NULL,
    quota_gb REAL NOT NULL,
    duration_days INTEGER,
    price INTEGER NOT NULL,
    node_id INTEGER,                 -- only set for 'new'
    node_name TEXT,
    protocol TEXT,
    target_username TEXT NOT NULL,   -- 'renew': existing username / 'new': proposed username
    status TEXT NOT NULL DEFAULT 'pending',
    receipt_file_id TEXT,
    created_at TEXT NOT NULL
);
"""

# Columns added after the table already shipped - ALTER'd in on top of an
# existing database in init_db() below (CREATE TABLE IF NOT EXISTS above is
# a no-op once the table already exists, so these never get created any
# other way on an upgrade). referral_code: invite code the customer typed
# in at checkout (brand-new "new" purchases only - see customer.py).
# discount_code/discount_amount: promo code applied, and how much it knocked
# off (0 if none). final_price: price AFTER the discount - what the
# card-to-card screen actually told the customer to pay; `price` above stays
# the original package price for record-keeping.
_NEW_COLUMNS = {
    "referral_code": "TEXT",
    "discount_code": "TEXT",
    "discount_amount": "INTEGER NOT NULL DEFAULT 0",
    "final_price": "INTEGER",
    # Which PaymentCard (see models.py) was actually shown to the customer
    # for THIS request (routers/bot.py's get_payment_info resolves one at
    # payment-screen time - see its docstring) - NULL if the relevant pool
    # had no registered cards (legacy single-card fallback was shown
    # instead) or this is a 'link' request (no payment involved at all).
    # Read back by admin_pending.py once the request is approved, to credit
    # the right card's accumulated_amount for "threshold" mode.
    "payment_card_id": "INTEGER",
    # For kind='renew': WHICH of the customer's independent services this
    # renewal continues (models.Purchase id, picked in the bot's «کدام
    # سرویس؟» step). NULL = customer had one/no service at request time -
    # the approval falls back to routers/bot.py renew()'s single-service
    # auto-target.
    "renew_purchase_id": "INTEGER",
    # Optional customer-written label for the service being bought (the
    # bot's «یک نام برای این سرویس» step) - applied to the created
    # models.Purchase on approval. See models.Purchase.comment.
    "comment": "TEXT",
    # WHICH panel account this request belongs to - the reseller whose bot
    # the customer was talking to (config.bot_owner_admin_id), or the
    # existing customer's owner for a renewal.
    #
    # Without it every admin's «درخواست‌های در انتظار» screen showed every
    # other admin's receipts, and any of them could approve a sale that
    # belonged to someone else's customer - the last place where the bot
    # still had no idea the hierarchy existed. NULL means the shared bot
    # with no reseller behind it, which only a superadmin sees, matching
    # how an ownerless customer is treated everywhere else.
    "owner_admin_id": "INTEGER",
}


@contextmanager
def _conn():
    # This one file is shared by EVERY reseller's dedicated bot, each
    # running on its own OS thread (see config.py's threading.local
    # RuntimeConfig) - under sqlite's default rollback-journal mode a
    # writer takes an exclusive lock on the whole file, and the default
    # busy_timeout is 0, so a second thread hitting this at the same
    # moment got an immediate, uncaught "database is locked"
    # sqlite3.OperationalError instead of just waiting its turn (found
    # during the 2026-09 full-codebase audit). WAL mode lets readers and
    # a writer proceed concurrently, and the busy_timeout below makes any
    # remaining writer-vs-writer contention wait a few seconds and retry
    # instead of failing outright. Both are cheap - WAL only needs to be
    # set once per database file, but re-issuing it on every connect is a
    # no-op and keeps this self-contained.
    conn = sqlite3.connect(config.db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Notices to admins that a receipt was APPROVED AUTOMATICALLY. Kept apart
# from pending_purchases and from anything financial: a row here only says
# "tell this chat about that approval"; resending it can never provision,
# credit or reward anything again. bot_id is the numeric part of the token
# of the bot that received the receipt - Telegram file ids are valid only
# for that bot, so only that bot instance delivers (and retries) the row.
# chat_id 0 = the recipients could not be resolved yet.
_NOTICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    bot_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    photo BLOB,                              -- only for a receipt with no Telegram file id (Mini App upload)
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | sending | sent | failed | gave_up
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    claimed_until TEXT,                      -- 'sending': taken by one sender until then (a crash frees it)
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE (request_id, chat_id)
);
"""


def init_db():
    with _conn() as conn:
        conn.execute(_SCHEMA)
        conn.execute(_NOTICE_SCHEMA)
        for col, coltype in _NEW_COLUMNS.items():
            try:
                conn.execute(f"ALTER TABLE pending_purchases ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass  # column already exists - normal on every restart after the first


def instance_id() -> str:
    """A random id of THIS storage file, made once and kept. Request ids
    are only unique inside one file, so (instance_id, request id) is what
    identifies a pending request to the panel (receipt-approval
    registration) - the shared bot, each remote installation and a
    restored copy all have their own."""
    with _conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT OR IGNORE INTO bot_meta (key, value) VALUES ('instance_id', ?)", (str(uuid.uuid4()),))
        return conn.execute("SELECT value FROM bot_meta WHERE key = 'instance_id'").fetchone()[0]


def create_pending(
    telegram_id: int,
    telegram_username: Optional[str],
    telegram_name: Optional[str],
    kind: str,
    package: dict,
    target_username: str,
    node_id: Optional[int] = None,
    node_name: Optional[str] = None,
    protocol: Optional[str] = None,
    receipt_file_id: Optional[str] = None,
    referral_code: Optional[str] = None,
    discount_code: Optional[str] = None,
    discount_amount: int = 0,
    final_price: Optional[int] = None,
    payment_card_id: Optional[int] = None,
    renew_purchase_id: Optional[int] = None,
    comment: Optional[str] = None,
    owner_admin_id: Optional[int] = None,
) -> int:
    # Defaults to the bot's own owner. Callers may pass an explicit value
    # (a renewal belongs to the customer's existing owner, which is not
    # necessarily the bot they happened to message from).
    if owner_admin_id is None:
        owner_admin_id = config.bot_owner_admin_id
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO pending_purchases
               (telegram_id, telegram_username, telegram_name, kind, package_id, package_name,
                quota_gb, duration_days, price, node_id, node_name, protocol, target_username,
                receipt_file_id, created_at, referral_code, discount_code, discount_amount, final_price,
                payment_card_id, renew_purchase_id, comment, owner_admin_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                telegram_id,
                telegram_username,
                telegram_name,
                kind,
                package["id"],
                package["name"],
                package.get("quota_gb", 0),
                package.get("duration_days"),
                package.get("price", 0),
                node_id,
                node_name,
                protocol,
                target_username,
                receipt_file_id,
                dt.datetime.utcnow().isoformat(),
                referral_code,
                discount_code,
                discount_amount or 0,
                final_price if final_price is not None else package.get("price", 0),
                payment_card_id,
                renew_purchase_id,
                comment,
                owner_admin_id,
            ),
        )
        return cur.lastrowid


def get_pending(request_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM pending_purchases WHERE id = ?", (request_id,)).fetchone()
        return dict(row) if row else None


def set_status(request_id: int, status: str):
    with _conn() as conn:
        conn.execute("UPDATE pending_purchases SET status = ? WHERE id = ?", (status, request_id))


def claim_pending(request_id: int) -> bool:
    """Atomically flips a request from 'pending' to 'processing' - used
    right before an admin's approve/reject click actually does anything, so
    two admins (or one double-tap) racing the same request can't both
    process it: the `WHERE status = 'pending'` only ever matches for
    whichever click gets there first (SQLite serializes writes), the loser
    sees rowcount 0 and backs off with "already handled" instead of
    double-provisioning/double-crediting. Returns True if this call won the
    claim. Callers should call set_status() with the real final status
    ('approved'/'rejected') once done, or release_pending() to put it back
    to 'pending' if processing failed and a retry should be allowed."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE pending_purchases SET status = 'processing' WHERE id = ? AND status = 'pending'",
            (request_id,),
        )
        return cur.rowcount > 0


def release_pending(request_id: int):
    """Puts a request claimed via claim_pending() back to 'pending' - used
    when approve/reject processing fails partway through, so the admin can
    retry instead of the request being stuck in 'processing' forever."""
    with _conn() as conn:
        conn.execute(
            "UPDATE pending_purchases SET status = 'pending' WHERE id = ? AND status = 'processing'",
            (request_id,),
        )


def _owner_clause(owner_ids: Optional[set[int]], include_unowned: bool) -> tuple[str, list]:
    """SQL fragment restricting rows to one admin's own requests.

    `owner_ids=None` means no restriction at all (the unscoped config-only
    admin - see admin_scope.ROLE_CONFIG_ONLY).

    `include_unowned` exists because NULL never matches an IN list in SQL,
    exactly as in routers/bot.py's _visibility_filter. Rows written before
    this column existed, and everything taken by the shared bot, have NULL
    here - a superadmin must still see those or their own pending screen
    would empty out on upgrade.
    """
    if owner_ids is None:
        return "", []
    parts: list[str] = []
    params: list = []
    if owner_ids:
        parts.append(f"owner_admin_id IN ({','.join('?' * len(owner_ids))})")
        params.extend(sorted(owner_ids))
    if include_unowned:
        parts.append("owner_admin_id IS NULL")
    if not parts:
        return " AND 1 = 0", []
    return " AND (" + " OR ".join(parts) + ")", params


def list_recent(
    limit: int = 15,
    owner_ids: Optional[set[int]] = None,
    include_unowned: bool = False,
) -> list[dict]:
    """The most recently HANDLED requests (approved/rejected) - the bot's
    admin «🗂 تاریخچه درخواست‌ها» screen. list_pending() below only ever
    shows what's still waiting, so once an admin acted there was no way to
    look back at what was decided."""
    where, params = _owner_clause(owner_ids, include_unowned)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_purchases WHERE status IN ('approved','rejected')"
            + where + " ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def list_pending(
    owner_ids: Optional[set[int]] = None,
    include_unowned: bool = False,
) -> list[dict]:
    where, params = _owner_clause(owner_ids, include_unowned)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_purchases WHERE status = 'pending'"
            + where + " ORDER BY id DESC",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def may_handle(request: dict, owner_ids: Optional[set[int]], include_unowned: bool) -> bool:
    """Single-request twin of the list filters above.

    Needed separately because approve/reject arrive as a callback carrying
    only a request id - the id is guessable and the button is reachable by
    anyone the bot considers an admin, so the check has to happen again at
    the moment of acting, not only when the list was drawn.
    """
    if owner_ids is None:
        return True
    owner = request.get("owner_admin_id")
    if owner is None:
        return include_unowned
    return owner in owner_ids


# --------------------------------------------------------------------------
# automatic-approval notices (see _NOTICE_SCHEMA)
# --------------------------------------------------------------------------
def _now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat()


def queue_notices(request_id: int, bot_id: str, chat_ids, text: str, photo: Optional[bytes] = None) -> None:
    """One row per recipient; queuing the same request again adds nothing."""
    now = _now_iso()
    with _conn() as conn:
        for chat_id in chat_ids:
            conn.execute(
                "INSERT OR IGNORE INTO approval_notices (request_id, bot_id, chat_id, text, photo, status, "
                "next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (request_id, bot_id, int(chat_id), text, photo, now, now),
            )


_DUE = ("(status IN ('pending', 'failed') AND next_attempt_at <= :now) "
        "OR (status = 'sending' AND claimed_until <= :now)")


def due_notices(bot_id: str, request_id: Optional[int] = None, limit: int = 20) -> list[dict]:
    """Due now - including a 'sending' row whose sender died (restart)."""
    query = f"SELECT * FROM approval_notices WHERE bot_id = :bot AND ({_DUE})"
    args: dict = {"bot": bot_id, "now": _now_iso(), "limit": limit}
    if request_id is not None:
        query += " AND request_id = :request"
        args["request"] = request_id
    with _conn() as conn:
        return [dict(r) for r in conn.execute(query + " ORDER BY id LIMIT :limit", args).fetchall()]


def claim_notice(notice_id: int, bot_id: str, lease_seconds: int) -> bool:
    """Take one due notice for sending - one atomic UPDATE, so the worker,
    the resend button and a restarted bot can never send it at once."""
    now = _now_iso()
    until = (dt.datetime.utcnow() + dt.timedelta(seconds=lease_seconds)).replace(microsecond=0).isoformat()
    with _conn() as conn:
        cur = conn.execute(f"UPDATE approval_notices SET status = 'sending', claimed_until = :until "
                           f"WHERE id = :id AND bot_id = :bot AND ({_DUE})",
                           {"until": until, "id": notice_id, "bot": bot_id, "now": now})
        return cur.rowcount == 1


def mark_notice(notice_id: int, status: str, *, error: Optional[str] = None,
                next_attempt_at: Optional[str] = None) -> None:
    """The outcome of a claimed send (only a 'sending' row changes)."""
    with _conn() as conn:
        conn.execute(
            "UPDATE approval_notices SET status = ?, attempts = attempts + 1, last_error = ?, claimed_until = NULL, "
            "next_attempt_at = COALESCE(?, next_attempt_at), "
            "sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END WHERE id = ? AND status = 'sending'",
            (status, (error or None) and error[:300], next_attempt_at, status, _now_iso(), notice_id),
        )


def close_resolver_notice(notice_id: int) -> None:
    """A chat_id 0 row whose recipients were resolved and queued."""
    with _conn() as conn:
        conn.execute("UPDATE approval_notices SET status = 'sent', sent_at = ?, claimed_until = NULL "
                     "WHERE id = ? AND status = 'sending'", (_now_iso(), notice_id))


def undelivered_notices(bot_id: str, owner_ids: Optional[set[int]], include_unowned: bool,
                        limit: int = 20) -> list[dict]:
    """Undelivered notices (retrying or stopped) of requests this admin may see."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT n.*, p.kind, p.target_username, p.owner_admin_id, p.final_price, p.price "
            "FROM approval_notices n JOIN pending_purchases p ON p.id = n.request_id "
            "WHERE n.bot_id = ? AND n.status IN ('failed', 'gave_up') ORDER BY n.id DESC LIMIT ?",
            (bot_id, limit * 4),
        ).fetchall()
    out = [dict(r) for r in rows if may_handle(dict(r), owner_ids, include_unowned)]
    return out[:limit]


def retry_notice(notice_id: int, bot_id: str) -> bool:
    """Manual retry: back to pending, due now. Attempts are kept. Never a
    row that is being sent right now or already sent."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE approval_notices SET status = 'pending', next_attempt_at = ? "
            "WHERE id = ? AND bot_id = ? AND status IN ('failed', 'gave_up')",
            (_now_iso(), notice_id, bot_id),
        )
        return cur.rowcount > 0


def get_notice(notice_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM approval_notices WHERE id = ?", (notice_id,)).fetchone()
        return dict(row) if row else None
