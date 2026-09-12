"""Importing the ready-made tutorials/adverts must never surprise anyone.

Run:  python3 backend/tests/test_import_defaults.py

The button ships content (app/data/defaults.py) into an account's own list.
Two properties make it safe to press, and both are easy to lose later:

  * idempotent by title - pressing twice adds nothing the second time;
  * additive only - it never edits or deletes what is already there, so an
    entry the admin changed keeps their changes.

Deletions are deliberately NOT remembered: re-importing brings a deleted
entry back, because the button means "give me the defaults I do not have".
That is also why this is a button rather than something seeded at startup -
nothing reappears unless someone asks for it.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from app import models
from app.data.defaults import DEFAULT_ADS, DEFAULT_TUTORIALS

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# The import logic itself, mirrored from routers/tutorials.py and
# routers/ads.py - kept here as the same few lines rather than by booting
# FastAPI, so this stays a unit test of the RULE.
def import_tutorials(db, owner_id):
    existing = {
        (t.title or "").strip()
        for t in db.query(models.Tutorial).filter(models.Tutorial.owner_admin_id == owner_id).all()
    }
    last = (db.query(func.max(models.Tutorial.sort_order))
            .filter(models.Tutorial.owner_admin_id == owner_id).scalar()) or 0
    added = 0
    for item in DEFAULT_TUTORIALS:
        title = (item.get("title") or "").strip()
        if not title or title in existing:
            continue
        last += 1
        db.add(models.Tutorial(title=title, text=item.get("text") or "", enabled=True,
                                sort_order=last, owner_admin_id=owner_id))
        added += 1
    db.commit()
    return added


def import_ads(db, channel_id):
    existing = {
        (p.title or "").strip()
        for p in db.query(models.AdPost).filter(models.AdPost.channel_id == channel_id).all()
    }
    last = (db.query(func.max(models.AdPost.sort_order))
            .filter(models.AdPost.channel_id == channel_id).scalar()) or 0
    added = 0
    for item in DEFAULT_ADS:
        title = (item.get("title") or "").strip()
        if not title or title in existing:
            continue
        last += 1
        db.add(models.AdPost(channel_id=channel_id, title=title, body=item.get("body") or "",
                              enabled=False, sort_order=last))
        added += 1
    db.commit()
    return added


print("--- the shipped content is usable as-is ---")
check("there are default tutorials", len(DEFAULT_TUTORIALS) > 0, True)
check("there are default adverts", len(DEFAULT_ADS) > 0, True)
check("every tutorial has a title and body",
      all((d.get("title") or "").strip() and (d.get("text") or "").strip() for d in DEFAULT_TUTORIALS), True)
check("every advert has a title and body",
      all((d.get("title") or "").strip() and (d.get("body") or "").strip() for d in DEFAULT_ADS), True)
check("no two tutorials share a title",
      len({d["title"] for d in DEFAULT_TUTORIALS}), len(DEFAULT_TUTORIALS))
check("no two adverts share a title",
      len({d["title"] for d in DEFAULT_ADS}), len(DEFAULT_ADS))

print("\n--- tutorials: import, then import again ---")
db = make_db()
check("the first import adds everything", import_tutorials(db, None), len(DEFAULT_TUTORIALS))
check("the second adds nothing", import_tutorials(db, None), 0)
check("...and did not duplicate anything",
      db.query(models.Tutorial).count(), len(DEFAULT_TUTORIALS))

print("\n--- an edited entry is left alone; a deleted one returns only if asked for ---")
edited = db.query(models.Tutorial).first()
edited.text = "متن ویرایش‌شده توسط ادمین"
removed = db.query(models.Tutorial).order_by(models.Tutorial.id.desc()).first()
removed_title = removed.title
db.delete(removed)
db.commit()

import_tutorials(db, None)
check("the admin's own edit survives re-importing",
      db.query(models.Tutorial).filter(models.Tutorial.id == edited.id).first().text,
      "متن ویرایش‌شده توسط ادمین")
check("a deleted entry comes back on an explicit re-import, and only then",
      db.query(models.Tutorial).filter(models.Tutorial.title == removed_title).count(), 1)

print("\n--- each owner gets their own copy ---")
db2 = make_db()
import_tutorials(db2, None)     # the superadmin's list
import_tutorials(db2, 7)        # a level-2 Admin's own list
check("the superadmin has their set",
      db2.query(models.Tutorial).filter(models.Tutorial.owner_admin_id.is_(None)).count(),
      len(DEFAULT_TUTORIALS))
check("and the admin has a separate one",
      db2.query(models.Tutorial).filter(models.Tutorial.owner_admin_id == 7).count(),
      len(DEFAULT_TUTORIALS))

print("\n--- adverts: same rules, and they arrive switched off ---")
db3 = make_db()
ch = models.AdChannel(id=1)
db3.add(ch)
db3.commit()
check("the first import adds everything", import_ads(db3, ch.id), len(DEFAULT_ADS))
check("the second adds nothing", import_ads(db3, ch.id), 0)
check("nothing starts broadcasting on its own",
      db3.query(models.AdPost).filter(models.AdPost.enabled.is_(True)).count(), 0)
check("they are ordered, not all at zero",
      sorted(p.sort_order for p in db3.query(models.AdPost).all()),
      list(range(1, len(DEFAULT_ADS) + 1)))

print("\n--- the advert templates only use placeholders that exist ---")
import re
from app.services.ads import PLACEHOLDERS

used = set()
for d in DEFAULT_ADS:
    used.update(re.findall(r"\{[a-z_]+\}", d["body"]))
check("no advert references an unknown placeholder", sorted(used - set(PLACEHOLDERS)), [])


print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("ایمپورت پیش‌فرض‌ها امن است: تکراری نمی‌سازد و ویرایش‌های ادمین را خراب نمی‌کند")
