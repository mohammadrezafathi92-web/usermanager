"""Tiny local sqlite store for pending purchase/renewal requests (the
card-to-card payment needs an admin to look at a receipt photo and click
Approve/Reject - this survives bot restarts between the customer sending
the receipt and an admin acting on it). Lives in the same /app/data volume
the panel's own database already uses, so it survives container
rebuilds/restarts just like everything else."""
import datetime as dt
import sqlite3
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


@contextmanager
def _conn():
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.execute(_SCHEMA)


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
) -> int:
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO pending_purchases
               (telegram_id, telegram_username, telegram_name, kind, package_id, package_name,
                quota_gb, duration_days, price, node_id, node_name, protocol, target_username,
                receipt_file_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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


def list_pending() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_purchases WHERE status = 'pending' ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
