"""Logging phase of the payment-card pools (Receipt Void design 15.2, P2).

  status   - every pool, its phase, and any counter that disagrees with its
             event log (read-only)
  capture  - legacy -> event_logged for every pool that has cards: the
             cards' current counters become the baseline
  revert   - event_logged -> legacy for every pool (the rollback; events
             stay and are ignored)

Run:  docker compose exec backend python -m app.scripts.card_pool_phase status
"""
from __future__ import annotations

import sys

from ..database import SessionLocal, engine
from ..services import payment_card_events as events
from ..services import receipt_void_schema


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "status"
    if action not in ("status", "capture", "revert"):
        print(__doc__)
        return 2
    state = receipt_void_schema.bootstrap(engine, SessionLocal)
    if not state.get("ready"):
        print("receipt void schema is NOT ready:", "; ".join(state.get("problems") or []))
        return 1
    db = SessionLocal()
    try:
        if action != "status":
            change = events.capture_baseline if action == "capture" else events.revert_to_legacy
            for pool in events.pool_report(db):
                if action == "capture" and not pool["cards"]:
                    continue
                result = change(db, pool["owner_admin_id"])
                db.commit()                      # one transaction per pool
                print(f"{result['pool_key']:<14} {'changed' if result['changed'] else 'unchanged'} -> {result['phase']}")
        drifted = 0
        for pool in events.pool_report(db):
            print(f"{pool['pool_key']:<14} phase={pool['phase']:<13} cards={pool['cards']}")
            for item in pool["drift"]:
                drifted += 1
                print(f"    DRIFT card {item['card_id']}: live={item['live']} from_events={item['from_events']}")
        return 1 if drifted else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
