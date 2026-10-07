"""PPTP uses the same editable PPP credentials as the other RADIUS protocols."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "sqlite://")
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app import models, schemas
from app.database import Base
from app.routers import users

engine = create_engine("sqlite://")
Base.metadata.create_all(engine)
with Session(engine) as db:
    user = models.User(username="pptp-password")
    node = models.Node(name="pptp-test", mt_host="127.0.0.1", type=models.NodeType.mikrotik)
    db.add_all([user, node])
    db.flush()
    conn = models.Connection(user_id=user.id, node_id=node.id, type=models.ConnectionType.pptp,
                             ppp_username="pptp-login", ppp_password="old-password")
    db.add(conn)
    db.commit()
    with patch.object(users, "_get_owned_user", return_value=user):
        users.update_connection(user.id, conn.id, schemas.ConnectionUpdate(ppp_password="new-password"), db=db, admin=None)
    db.expire_all()
    assert db.get(models.Connection, conn.id).ppp_password == "new-password"
    assert db.get(models.Connection, conn.id).ppp_username == "pptp-login"
    print("PASS PPTP password persists without changing username or making node calls")

page = (Path(__file__).resolve().parents[2] / "frontend/src/pages/UserDetail.jsx").read_text()
edit_guard = page.split('title={t("userDetail.editConnTitle")}')[0].rsplit("&& (", 1)[0].rsplit("{", 1)[1]
assert '"pptp"' in edit_guard and ".includes(c.type)" in edit_guard
assert 'payload.ppp_password = editConnForm.ppp_password.trim()' in page
print("PASS PPTP edit button exposes the existing PPP password form")
engine.dispose()
