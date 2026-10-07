"""DB-clock lease fencing (Receipt Void 8 / Lifecycle 10.1).

Caller commits acquisition/renew/release separately and releases only after
the whole business transaction commits or rolls back. Never holds a business
transaction across remote I/O. No live caller or mode activation yet.
"""
import re
from dataclasses import dataclass

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .. import models_receipt_void as rv
from . import receipt_void_schema

KEY = re.compile(r"(?:customer_identity:[1-9][0-9]*|payment_card_pool:(?:global|admin:[1-9][0-9]*)|node:[1-9][0-9]*:wg_pool|provisioning_op:[1-9][0-9]*)")


class LeaseBusy(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class LeaseProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    resource_key: str
    owner: str
    epoch: int

    def __post_init__(self):
        if not isinstance(self.resource_key, str) or len(self.resource_key) > 96 or not KEY.fullmatch(self.resource_key):
            raise LeaseProtocolError("invalid_resource_key")
        if not isinstance(self.owner, str) or len(self.owner) > 64 or not re.fullmatch(r"op:[1-9][0-9]*:[1-9][0-9]*", self.owner):
            raise LeaseProtocolError("invalid_lease_owner")
        if type(self.epoch) is not int or self.epoch < 1:
            raise LeaseProtocolError("invalid_fencing_epoch")


def _clock(db, ttl=None):
    receipt_void_schema.assert_ready()
    dialect = db.get_bind().dialect.name
    if ttl is not None and (type(ttl) is not int or not 1 <= ttl <= 3600):
        raise LeaseProtocolError("invalid_lease_ttl")
    if dialect == "sqlite":
        return text("strftime('%Y-%m-%d %H:%M:%f','now')"), (
            text("strftime('%Y-%m-%d %H:%M:%f','now',:lease_modifier)")
            .bindparams(lease_modifier=f"+{ttl} seconds") if ttl is not None else None)
    if dialect in ("mysql", "mariadb"):
        return text("NOW(6)"), (text("TIMESTAMPADD(SECOND,:lease_ttl,NOW(6))")
                                .bindparams(lease_ttl=ttl) if ttl is not None else None)
    raise LeaseProtocolError("unsupported_lease_database")


def acquire(db, resource_key, owner, ttl=60):
    Lease(resource_key, owner, 1)  # Validate before inserting anything.
    now, deadline = _clock(db, ttl)
    table = rv.resource_locks
    if db.get_bind().dialect.name == "sqlite":
        statement = sqlite_insert(table).values(resource_key=resource_key).on_conflict_do_nothing(
            index_elements=[table.c.resource_key])
    else:
        statement = mysql_insert(table).values(resource_key=resource_key).on_duplicate_key_update(
            resource_key=table.c.resource_key)
    db.execute(statement)
    result = db.execute(table.update().where(table.c.resource_key == resource_key,
        or_(table.c.lease_owner.is_(None), table.c.leased_until < now)
    ).values(lease_owner=owner, fencing_epoch=table.c.fencing_epoch + 1,
             leased_until=deadline, heartbeat_at=now, version=table.c.version + 1))
    if result.rowcount != 1:
        raise LeaseBusy("resource_lease_busy")
    epoch = db.execute(select(table.c.fencing_epoch).where(table.c.resource_key == resource_key)).scalar_one()
    return Lease(resource_key, owner, epoch)


def renew(db, lease, ttl=60):
    if not isinstance(lease, Lease):
        raise LeaseProtocolError("invalid_lease")
    now, deadline = _clock(db, ttl)
    table = rv.resource_locks
    result = db.execute(table.update().where(table.c.resource_key == lease.resource_key,
        table.c.lease_owner == lease.owner, table.c.fencing_epoch == lease.epoch,
        table.c.leased_until >= now).values(leased_until=deadline, heartbeat_at=now, version=table.c.version + 1))
    if result.rowcount != 1:
        raise LeaseLost("resource_lease_lost")


def release(db, lease):
    if not isinstance(lease, Lease):
        raise LeaseProtocolError("invalid_lease")
    _clock(db)
    table = rv.resource_locks
    result = db.execute(table.update().where(table.c.resource_key == lease.resource_key,
        table.c.lease_owner == lease.owner, table.c.fencing_epoch == lease.epoch
    ).values(lease_owner=None, leased_until=None, version=table.c.version + 1))
    return result.rowcount == 1


def begin_business(db):
    """Must be first, before runtime/lease locking reads or other SQL."""
    if db.in_transaction():
        raise LeaseProtocolError("business_transaction_already_started")
    _clock(db)
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    else:
        db.begin()
    db.info["resource_lease_business_transaction"] = db.get_transaction()


def revalidate(db, leases):
    """Lock every row and check owner + epoch + DB-clock liveness."""
    tokens = tuple(leases)
    if not tokens or any(not isinstance(lease, Lease) for lease in tokens):
        raise LeaseProtocolError("invalid_lease_set")
    expected = {lease.resource_key: lease for lease in tokens}
    if len(expected) != len(tokens):
        raise LeaseProtocolError("duplicate_resource_lease")
    if db.get_transaction() is None or db.info.get("resource_lease_business_transaction") is not db.get_transaction():
        raise LeaseProtocolError("business_transaction_not_fenced")
    now, _ = _clock(db)
    table = rv.resource_locks
    rows = db.execute(select(table.c.resource_key, table.c.lease_owner, table.c.fencing_epoch,
                             (table.c.leased_until >= now).label("live"))
                      .where(table.c.resource_key.in_(sorted(expected))).order_by(table.c.resource_key)
                      .with_for_update()).mappings().all()
    if len(rows) != len(expected) or any((row["lease_owner"], row["fencing_epoch"], bool(row["live"])) != (
            expected[row["resource_key"]].owner, expected[row["resource_key"]].epoch, True) for row in rows):
        raise LeaseLost("resource_lease_lost")
