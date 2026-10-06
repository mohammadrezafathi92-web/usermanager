"""The only writer of the customer wallet (User.balance).

Receipt Void design, phase P5 (sections 3.6 item 1, 9.1, 9.2): every place
that used to write User.balance now goes through this module, so that the
wallet has one door before the cutover (P9) replaces what is behind it.
tests/test_wallet_single_writer.py (RV-60) fails the build if any other
module writes the column.

Only the LEGACY generation exists here: it writes User.balance directly and
is allowed only while wallet_runtime_state.phase is 'normal'. The wallet-lot
generation (sources, lots, debts) is P9 and is not implemented; nothing in
this module creates an account, a source or a lot, and nothing here can
reverse a credit. AdminUser.balance (reseller credit) is a separate system
and is not touched.

What each function keeps exactly as it was before the move:
  adjust_in_session / overwrite   plain attribute writes on the loaded User,
                                  committed by the caller with the rest of
                                  its work (loyalty, referral, panel edit);
  credit_atomic / debit_atomic    one UPDATE statement, so concurrent
                                  requests cannot lose or overdraw.

Phase contract (9.2). A writer may only run in a phase that allows its
generation, decided by a current read of the singleton row:
  - MariaDB: a shared locking read, held to the caller's commit, so a phase
    change waits for this writer and later writers see the new phase.
  - SQLite: the top-up and debit endpoints become the writer first (BEGIN
    IMMEDIATE: begin_writer, and receipt_approval_topup's own start), so
    their read is current and a phase change waits for them. The rewards
    and the panel edit write in the middle of a larger transaction that may
    talk to a router, and must not hold the database's single writer for
    that long. A read there is only an early refusal: Python's sqlite3 does
    not open a transaction for a SELECT, so the phase could change before
    the write is flushed. Those writes are therefore checked again inside
    the flush that carries them (_recheck_phase_in_flush): by then the
    session IS the writer, the read is current, and a refusal fails the
    flush, so nothing of that transaction is committed.
While the Receipt Void schema is not ready there is no phase row and the
legacy writer runs as it always did. No code changes the phase before P9.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from .. import models
from . import receipt_void_schema

log = logging.getLogger(__name__)

LEGACY_PHASE = "normal"

# Why the balance changes. The first four are the credit origins of the
# design (11.1); "sale" is its debit kind for a purchase paid from the
# wallet. "unspecified" is an add-balance request that names no reason:
# still accepted for both signs until P7 makes source_kind mandatory there.
RECEIPT_TOPUP = "receipt_topup"
LOYALTY_REWARD = "loyalty_reward"
REFERRAL_REWARD = "referral_reward"
MANUAL_ADJUSTMENT = "manual_adjustment"
SALE = "sale"
UNSPECIFIED = "unspecified"
SOURCE_KINDS = (RECEIPT_TOPUP, LOYALTY_REWARD, REFERRAL_REWARD, MANUAL_ADJUSTMENT, SALE, UNSPECIFIED)
CREDIT_KINDS = (RECEIPT_TOPUP, LOYALTY_REWARD, REFERRAL_REWARD, MANUAL_ADJUSTMENT, UNSPECIFIED)
DEBIT_KINDS = (SALE, MANUAL_ADJUSTMENT, UNSPECIFIED)


class LegacyWriterUnavailable(HTTPException):
    """The wallet is not in the phase that allows the legacy writer."""

    def __init__(self) -> None:
        super().__init__(status_code=503, detail="wallet_legacy_writer_unavailable")


def _phase_table():
    from .. import models_receipt_void as rv
    return rv.wallet_runtime_state


def require_legacy_phase(db: Session) -> None:
    """Current read of the wallet phase; anything but 'normal' refuses."""
    if not receipt_void_schema.is_ready():
        return
    table = _phase_table()
    query = select(table.c.phase).where(table.c.id == receipt_void_schema.SINGLETON_ID)
    if db.get_bind().dialect.name != "sqlite":
        query = query.with_for_update(read=True)
    if db.execute(query).scalar() != LEGACY_PHASE:
        raise LegacyWriterUnavailable()


_RECHECK = "wallet_legacy_phase_recheck"


def _written_in_session(db: Session) -> None:
    """Mark a balance change that waits in the session for the caller's flush."""
    db.info[_RECHECK] = True


@event.listens_for(Session, "after_flush")
def _recheck_phase_in_flush(session: Session, _flush_context) -> None:
    """The phase, read again inside the transaction that writes the balance.

    Needed on SQLite only (see the module docstring); on MariaDB the shared
    row lock taken by the first read is still held."""
    if not session.info.pop(_RECHECK, False):
        return
    if session.get_bind().dialect.name == "sqlite":
        require_legacy_phase(session)


def begin_writer(db: Session) -> None:
    """Start a transaction that only writes the wallet (9.2): end whatever
    the request's dependencies read, become the writer on SQLite, then take
    the phase read as the first statement. For short transactions with no
    network call inside - never around provisioning."""
    db.rollback()
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    require_legacy_phase(db)


def _kind(source_kind: str, allowed: tuple) -> None:
    if source_kind not in allowed:
        raise ValueError(f"wallet source_kind {source_kind!r} is not one of {allowed}")


def _positive(amount: int) -> int:
    amount = int(amount)
    if amount <= 0:
        raise ValueError("a wallet amount must be positive; the direction is the function")
    return amount


def adjust_in_session(db: Session, user: models.User, delta: int, *, source_kind: str) -> int:
    """Add `delta` to the loaded User's balance; the caller commits. Returns
    the new balance. For the loyalty and referral rewards, whose amount is a
    panel setting: the setting has never been range-checked, so a negative
    one still subtracts exactly as it did before this module existed."""
    _kind(source_kind, (LOYALTY_REWARD, REFERRAL_REWARD))
    delta = int(delta)
    if delta == 0:
        return int(user.balance or 0)
    require_legacy_phase(db)
    user.balance = (user.balance or 0) + delta
    _written_in_session(db)
    return user.balance


def overwrite(db: Session, user: models.User, new_balance: Optional[int], *, source_kind: str) -> None:
    """Set the loaded User's balance to an absolute value; the caller commits.

    The panel's edit form and the test-sale cleanup script still work this
    way. P7 removes the form's field in favour of a manual-adjustment
    endpoint with a reason and an idempotency token."""
    _kind(source_kind, (MANUAL_ADJUSTMENT,))
    require_legacy_phase(db)
    user.balance = new_balance
    _written_in_session(db)


def credit_atomic(db: Session, user_id: int, amount: int, *, source_kind: str,
                  phase_checked: bool = False) -> Optional[int]:
    """One UPDATE: balance = balance + amount. Returns the balance after it
    (a current locking read), or None if there is no such customer. The
    caller commits. `phase_checked` is for a caller that already took the
    phase read as the first statement of this same transaction."""
    _kind(source_kind, CREDIT_KINDS)
    amount = _positive(amount)
    if not phase_checked:
        require_legacy_phase(db)
    users = models.User.__table__
    result = db.execute(users.update().where(users.c.id == user_id).values(
        balance=func.coalesce(users.c.balance, 0) + amount))
    if result.rowcount != 1:
        return None
    return int(db.execute(select(users.c.balance).where(users.c.id == user_id).with_for_update()).scalar() or 0)


def debit_atomic(db: Session, user_id: int, amount: int, *, source_kind: str) -> bool:
    """One conditional UPDATE: take `amount` only if the balance covers it.
    False means it did not (or there is no such customer) and nothing
    changed - two requests can never both succeed past zero. The caller
    commits."""
    _kind(source_kind, DEBIT_KINDS)
    amount = _positive(amount)
    require_legacy_phase(db)
    users = models.User.__table__
    result = db.execute(users.update().where(
        users.c.id == user_id, (users.c.balance - amount) >= 0).values(balance=users.c.balance - amount))
    return result.rowcount == 1
