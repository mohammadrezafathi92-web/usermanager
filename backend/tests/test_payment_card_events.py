"""Receipt Void phase P2 - per-pool baseline and the causal payment event
log (design sections 15.1-15.4, P2 row of section 21).

Covers: a 'legacy' pool logs nothing and behaves as before; capture is
atomic and re-capturable; the reference simulation of 15.4; a failing
event never fails the payment (pool falls back to 'legacy'); new pools
start event_logged; card delete detaches events.
Run:  python3 backend/tests/test_payment_card_events.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models, models_receipt_void as rv
from app.services import payment_card_events as events
from app.services import payment_cards as cards
from app.services import receipt_void_schema

failures: list[str] = []
E, S, B = rv.payment_card_pool_events, rv.payment_card_pool_states, rv.payment_card_pool_baselines


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


folder = tempfile.mkdtemp()
engine = create_engine(f"sqlite:///{folder}/t.db")
models.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
db = Session()
db.add(models.PanelSettings(id=1))
reseller = models.AdminUser(username="res", hashed_password="x", is_superadmin=False)
db.add(reseller)
db.commit()


def event_rows(key="global"):
    return [dict(r._mapping) for r in db.execute(select(E).where(E.c.pool_key == key).order_by(E.c.id))]


def counters(*card_objs):
    db.expire_all()
    return tuple(db.get(models.PaymentCard, c.id).accumulated_amount or 0 for c in card_objs)


def pointer():
    db.expire_all()
    return db.get(models.PanelSettings, 1).active_payment_card_id


def phase(key="global"):
    return db.execute(select(S.c.logging_phase).where(S.c.pool_key == key)).scalar()


print("--- schema not ready: pure legacy, the new tables are never touched ---")
check("not ready before bootstrap", events.enabled(), False)
a = cards.create_card(db, None, {"card_number": "6037-0000-0000-1111", "card_holder": "Ali"})
db.commit()
cards.advance_after_payment(db, a.id, 10)
check("payment works with no event tables at all", counters(a), (10,))

state = receipt_void_schema.bootstrap(engine, Session)
check("schema ready after bootstrap", (state["ready"], events.enabled()), (True, True))

print("--- a legacy pool: counter moves, nothing is logged ---")
b = cards.create_card(db, None, {"card_number": "6037-0000-0000-2222", "card_holder": "Bita", "sort_order": 1})
c = cards.create_card(db, None, {"card_number": "6037-0000-0000-3333", "card_holder": "Cyrus", "sort_order": 2})
db.commit()
check("an existing pool is NOT switched on by adding cards to it", phase(), None)
cards.advance_after_payment(db, a.id, 5)
check("legacy payment: counter moved, state row created as legacy, no event",
      (counters(a), phase(), event_rows()), ((15,), "legacy", []))

print("--- capture ---")
result = events.capture_baseline(db, None)
db.commit()
baseline = json.loads(db.execute(select(B.c.accumulated_by_card)).scalar())
check("capture: phase, and the baseline is every card's live counter",
      (result["changed"], phase(), baseline), (True, "event_logged", {str(a.id): 15, str(b.id): 0, str(c.id): 0, "_after_event_id": 0}))
check("capturing again is a no-op", events.capture_baseline(db, None)["changed"], False)
db.rollback()
events.capture_baseline(db, reseller.id)
db.rollback()
check("a rolled-back capture switches nothing on and leaves no baseline (at most an inert 'legacy' state row)",
      (phase(f"admin:{reseller.id}") in (None, "legacy"),
       db.execute(select(B.c.pool_key).where(B.c.pool_key == f"admin:{reseller.id}")).first()), (True, None))
db.execute(S.delete().where(S.c.pool_key == f"admin:{reseller.id}"))
db.commit()

print("--- reference simulation, design 15.4 (threshold 100, order A,B,C) ---")
db.get(models.PaymentCard, a.id).accumulated_amount = 0
db.commit()
events.revert_to_legacy(db, None); events.capture_baseline(db, None)
cards.set_pool_settings(db, None, mode="threshold", switch_threshold=100, active_card_id=a.id)
db.commit()
for card, amount, uuid in ((a, 60, None), (a, 50, "00000000-0000-0000-0000-00000000e2e2"), (b, 70, None), (b, 40, None), (c, 30, None)):
    cards.advance_after_payment_core(db, card.id, amount, approval_uuid=uuid)
    db.commit()
rows = event_rows()
check("live result: A=0, B=0, C=30, active=C", (counters(a, b, c), pointer()), ((0, 0, 30), c.id))
check("five events with the causal columns of the table in 15.4",
      [(r["card_id_snapshot"], r["amount"], r["accumulated_before"], bool(r["reset_after"]), r["rotated_to_card_id_snapshot"],
        r["active_card_id_before"]) for r in rows],
      [(a.id, 60, 0, False, None, a.id), (a.id, 50, 60, True, b.id, a.id), (b.id, 70, 0, False, None, b.id),
       (b.id, 40, 70, True, c.id, b.id), (c.id, 30, 0, False, None, c.id)])
check("kind follows the approval uuid; snapshots are stored",
      ([r["event_kind"] for r in rows][:2], rows[1]["mode_snapshot"], rows[1]["threshold_snapshot"],
       json.loads(rows[1]["card_order_snapshot"])),
      (["payment_recorded_uncorrelated", "payment_recorded"], "threshold", 100, [a.id, b.id, c.id]))
check("the label is last four digits + holder, never the full number", rows[0]["card_label_snapshot"], "1111 Ali")
check("accumulator == live counter for every card", [events.accumulator(db, None, x.id) for x in (a, b, c)], [0, 0, 30])
check("the report shows no drift", [p["drift"] for p in events.pool_report(db) if p["pool_key"] == "global"], [[]])
db.execute(E.update().where(E.c.id == rows[1]["id"]).values(voided_at=events._now()))
check("voiding e2 recomputes only card A: A=60, B=0, C=30",
      [events.accumulator(db, None, x.id) for x in (a, b, c)], [60, 0, 30])
check("...and the report now shows exactly that drift",
      [p["drift"] for p in events.pool_report(db) if p["pool_key"] == "global"], [[{"card_id": a.id, "live": 0, "from_events": 60}]])
db.rollback()

print("--- event and counter are one transaction ---")
before = len(event_rows())
cards.advance_after_payment_core(db, c.id, 7)
db.rollback()
check("rollback undoes the counter AND the event", (counters(c), len(event_rows())), ((30,), before))

print("--- revert, then capture again: old events are ignored ---")
events.revert_to_legacy(db, None)
db.commit()
cards.advance_after_payment(db, c.id, 20)
check("legacy again: counter moves, no new event", (counters(c), len(event_rows())), ((50,), before))
events.capture_baseline(db, None)
db.commit()
cards.advance_after_payment(db, c.id, 1)
check("new baseline + only the new event", (events.accumulator(db, None, c.id), counters(c), len(event_rows())), (51, (51,), before + 1))

print("--- a failing event never fails the payment ---")
real_label = events.card_label
events.card_label = lambda card: 1 / 0
try:
    wrote = cards.advance_after_payment_core(db, c.id, 4)
    db.commit()
finally:
    events.card_label = real_label
check("payment still recorded, pool fell back to legacy, no half-written event",
      (wrote, counters(c), phase(), len(event_rows())), (True, (55,), "legacy", before + 1))
events.capture_baseline(db, None)
db.commit()
db.get(models.PanelSettings, 1).payment_card_switch_threshold = -5
db.get(models.PanelSettings, 1).active_payment_card_id = c.id
db.commit()
cards.advance_after_payment(db, c.id, 1)
check("an event the database rejects (CHECK) also only demotes the pool; the live reset still happened",
      (counters(c), phase()), ((0,), "legacy"))

print("--- event AND demotion both fail: the counter write is thrown away too ---")
events.capture_baseline(db, None)
db.get(models.PanelSettings, 1).payment_card_switch_threshold = 1000
db.commit()
counter_before = counters(c)[0]
events_before = len(event_rows())
real_label, real_set_phase = events.card_label, events._set_phase
events.card_label = lambda card: 1 / 0


def broken_set_phase(*args, **kwargs):
    raise RuntimeError("state row unavailable")


events._set_phase = broken_set_phase
try:
    try:
        cards.advance_after_payment(db, c.id, 10)
        outcome = "returned"
    except events.PoolLogBroken:
        outcome = "PoolLogBroken"
finally:
    events.card_label, events._set_phase = real_label, real_set_phase
check("it raises, and nothing is left behind: same counter, no event, pool still event_logged and consistent",
      (outcome, counters(c)[0], len(event_rows()), phase(), events.accumulator(db, None, c.id) == counters(c)[0]),
      ("PoolLogBroken", counter_before, events_before, "event_logged", True))
events.revert_to_legacy(db, None)
db.commit()

print("--- a brand new pool starts event_logged ---")
mine = cards.create_card(db, reseller.id, {"card_number": "5022-9999", "card_holder": "R"})
db.commit()
key = f"admin:{reseller.id}"
check("first card of a never-seen pool: event_logged with an empty baseline",
      (phase(key), json.loads(db.execute(select(B.c.accumulated_by_card).where(B.c.pool_key == key)).scalar())),
      ("event_logged", {str(mine.id): 0, "_after_event_id": 0}))
cards.advance_after_payment(db, mine.id, 9)
check("its payments are events from the first one", (events.accumulator(db, reseller.id, mine.id), len(event_rows(key))), (9, 1))

print("--- deleting a card keeps its events ---")
cards.delete_card(db, mine)
db.commit()
row = event_rows(key)[0]
check("card_id is cleared, card_id_snapshot stays", (row["card_id"], row["card_id_snapshot"]), (None, mine.id))
again = cards.create_card(db, reseller.id, {"card_number": "5022-8888", "card_holder": "R"})
db.commit()
check("the pool keeps its phase when it gets a first card again", phase(key), "event_logged")
check("a new card that reuses the deleted card's id (SQLite) inherits neither its events nor its baseline entry",
      (again.id == mine.id, events.accumulator(db, reseller.id, again.id),
       json.loads(db.execute(select(B.c.accumulated_by_card).where(B.c.pool_key == key)).scalar())),
      (True, 0, {"_after_event_id": 0}))

print("--- the management command ---")
from app.scripts import card_pool_phase
import app.scripts.card_pool_phase as cli
cli.engine, cli.SessionLocal = engine, Session
check("status exits 0 when nothing drifts", cli.main(["x", "status"]), 0)
check("capture switches the legacy pool on", (cli.main(["x", "capture"]), phase()), (0, "event_logged"))
check("revert switches every pool off", (cli.main(["x", "revert"]), phase(), phase(key)), (0, "legacy", "legacy"))
check("unknown action", cli.main(["x", "nope"]), 2)

db.close()

print("--- a table created with the too-short event_kind column is repaired ---")
from sqlalchemy import inspect as sa_inspect, text as sa_text


def repaired_width(target):
    """Puts event_kind back to the old VARCHAR(28), bootstraps, returns
    (schema ready, column length now)."""
    with target.begin() as conn:
        if target.dialect.name == "sqlite":
            conn.execute(sa_text("DROP TABLE payment_card_pool_events"))
            E.c.event_kind.type.length = 28
            try:
                E.create(conn)
            finally:
                E.c.event_kind.type.length = 32
        else:
            conn.execute(sa_text("DELETE FROM payment_card_pool_events"))
            conn.execute(sa_text("ALTER TABLE payment_card_pool_events MODIFY event_kind VARCHAR(28) NOT NULL"))
    ready = receipt_void_schema.bootstrap(target, sessionmaker(bind=target))["ready"]
    column = next(c for c in sa_inspect(target).get_columns("payment_card_pool_events") if c["name"] == "event_kind")
    return ready, column["type"].length


check("SQLite: old shape -> ready again, width 32", repaired_width(engine), (True, 32))

print("--- the same path on a real MariaDB (locking reads, savepoints, CHECKs) ---")
mariadb_url = os.environ.get("MARIADB_TEST_URL", "").strip()
if mariadb_url:
    maria = create_engine(mariadb_url)

    def drop_everything():
        with maria.begin() as conn:
            conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
            for name in sa_inspect(conn).get_table_names():
                conn.exec_driver_sql(f"DROP TABLE IF EXISTS `{name}`")
            conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")

    drop_everything()
    try:
        models.Base.metadata.create_all(maria)
        MSession = sessionmaker(bind=maria)
        check("MariaDB: schema ready", receipt_void_schema.bootstrap(maria, MSession)["ready"], True)
        db = MSession()
        db.add(models.PanelSettings(id=1))
        db.commit()
        x = cards.create_card(db, None, {"card_number": "1111", "card_holder": "X"})
        y = cards.create_card(db, None, {"card_number": "2222", "card_holder": "Y", "sort_order": 1})
        cards.set_pool_settings(db, None, mode="threshold", switch_threshold=100)
        db.commit()
        check("MariaDB: a never-seen pool starts event_logged", phase(), "event_logged")
        for amount in (60, 50):
            cards.advance_after_payment(db, x.id, amount)
        cards.advance_after_payment(db, y.id, 30)
        check("MariaDB: live counters, pointer, events and accumulator agree",
              (counters(x, y), pointer(), [bool(r["reset_after"]) for r in event_rows()],
               [events.accumulator(db, None, card.id) for card in (x, y)], phase()),
              ((0, 30), y.id, [False, True, False], [0, 30], "event_logged"))
        events.revert_to_legacy(db, None)
        events.capture_baseline(db, None)
        db.commit()
        cards.advance_after_payment(db, y.id, 5)
        check("MariaDB: re-capture ignores the earlier events", (events.accumulator(db, None, y.id), counters(y)), (35, (35,)))
        cards.delete_card(db, x)
        db.commit()
        check("MariaDB: deleting a card keeps its events, detached",
              [(r["card_id"], r["card_id_snapshot"]) for r in event_rows()][:2], [(None, x.id), (None, x.id)])
        db.close()
        check("MariaDB: old shape -> ready again, width 32", repaired_width(maria), (True, 32))
    finally:
        drop_everything()
        maria.dispose()
elif os.environ.get("CI"):
    check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
else:
    print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI)")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
