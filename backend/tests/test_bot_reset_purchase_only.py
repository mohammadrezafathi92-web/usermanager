"""The bot's username-only reset cannot mutate the obsolete shared pool."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

import _no_network
_no_network.install([])

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.routers import bot
from app.services.bot_auth import BotPrincipal


GB = 1024 ** 3
engine = create_engine("sqlite://")
models.Base.metadata.create_all(engine)
Factory = sessionmaker(bind=engine)
principal = BotPrincipal.internal(None)

with Factory() as db:
    one = models.User(username="one", total_quota_bytes=10 * GB, used_bytes=5 * GB)
    many = models.User(username="many", total_quota_bytes=20 * GB, used_bytes=7 * GB)
    empty = models.User(username="empty", total_quota_bytes=10 * GB, used_bytes=4 * GB)
    legacy = models.User(username="legacy", total_quota_bytes=10 * GB, used_bytes=3 * GB)
    node = models.Node(name="n", type=models.NodeType.mikrotik)
    db.add_all([one, many, empty, legacy, node])
    db.flush()
    first = models.Purchase(user_id=one.id, quota_bytes=10 * GB, used_bytes=5 * GB)
    db.add_all([first,
        models.Purchase(user_id=many.id, quota_bytes=10 * GB, used_bytes=2 * GB),
        models.Purchase(user_id=many.id, quota_bytes=10 * GB, used_bytes=5 * GB),
        models.Connection(user_id=legacy.id, node_id=node.id,
            type=models.ConnectionType.l2tp, ppp_username="legacy", ppp_password="x"),
    ])
    db.commit()
    first_id = first.id

with Factory() as db:
    result = bot.reset_usage("one", db=db, principal=principal)
    assert result.used_bytes == 0
    assert db.get(models.Purchase, first_id).used_bytes == 0
    assert db.query(models.User).filter_by(username="one").one().used_bytes == 5 * GB

with Factory() as db:
    for username, code in (("many", "service_selection_required"),
                           ("empty", "service_purchase_required"),
                           ("legacy", "legacy_service_migration_required")):
        try:
            bot.reset_usage(username, db=db, principal=principal)
        except HTTPException as exc:
            assert (exc.status_code, exc.detail) == (503 if username == "legacy" else 409, code)
        else:
            raise AssertionError(f"{username}: reset unexpectedly accepted")
        db.rollback()
    assert [p.used_bytes for p in db.query(models.Purchase).filter(
        models.Purchase.user_id == db.query(models.User.id).filter_by(username="many").scalar()
    ).order_by(models.Purchase.id)] == [2 * GB, 5 * GB]
    assert db.query(models.User).filter_by(username="empty").one().used_bytes == 4 * GB
    assert db.query(models.User).filter_by(username="legacy").one().used_bytes == 3 * GB

assert _no_network.attempts == []
print("PASS bot reset only touches one unambiguous Purchase")
