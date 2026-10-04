"""A backup too large for one Telegram upload is sent in pieces and can be
restored from them.

Run:  python3 backend/tests/test_backup_split.py

Telegram refuses any file over 50MB from a bot. A database past that size
used to be backed up on disk and simply never arrive - the admins only got
a notice saying so. Now the file is sent as plain byte slices
(NAME.partNNofMM); joining them in order is the original file, and the
panel's restore accepts all slices at once.
"""
from __future__ import annotations

import gzip
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import backup
from app.telegram_bot import runner

failures: list[str] = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def refused(files):
    try:
        backup.join_parts(files)
    except ValueError:
        return True
    return False


folder = Path(tempfile.mkdtemp())
payload = os.urandom(2500)
big = folder / "backup_20260101_000000.db.gz"
big.write_bytes(payload)

print("--- splitting ---")
check("a file that fits is sent as it is", backup.split_for_telegram(big, part_bytes=5000), [big])
parts = backup.split_for_telegram(big, part_bytes=1000)
check("2500 bytes in 1000-byte slices: three parts, named in order",
      ([p.name for p in parts], [p.stat().st_size for p in parts]),
      ([f"{big.name}.part01of03", f"{big.name}.part02of03", f"{big.name}.part03of03"], [1000, 1000, 500]))
check("an exact multiple makes no empty last part", len(backup.split_for_telegram(big, part_bytes=1250)), 2)
check("pieces are well under Telegram's 50MB (a 45MB piece did not get through a proxy in production)",
      backup.TELEGRAM_PART_BYTES <= 20 * 1024 * 1024, True)
check("the upload timeout grows with the piece: 90s base, a 19MB piece gets over six minutes",
      (backup._send_timeout(0), backup._send_timeout(19 * 1024 * 1024) > 360), (90.0, True))

print("--- joining ---")
uploads = [(p.name, p.read_bytes()) for p in parts]
check("all parts, in any order, give back the exact file",
      (backup.join_parts(uploads) == payload, backup.join_parts(list(reversed(uploads))) == payload), (True, True))
check("a browser path in the filename does not matter",
      backup.join_parts([("dir/" + n, d) for n, d in uploads]) == payload, True)
check("one ordinary file passes through untouched", backup.join_parts([("backup.db.gz", b"abc")]), b"abc")
other = [(n.replace("20260101", "20250505"), d) for n, d in uploads]
check("refused: a missing part, a repeated part, parts of two backups, a stray file among parts, nothing at all",
      [refused(uploads[:2]), refused(uploads + uploads[:1]), refused(uploads[:2] + other[2:]),
       refused(uploads + [("notes.txt", b"x")]), refused([])], [True] * 5)
check("refused: one lone part of a multi-part backup", refused(uploads[:1]), True)

print("--- sending ---")
import shutil
for leftover in folder.glob(".parts_*"):          # made by the direct split calls above
    shutil.rmtree(leftover)
sent_files, sent_texts = [], []
backup._admin_telegram_ids = lambda: [11, 22]
runner.send_document_sync = lambda chat_id, path, caption="", **kw: sent_files.append((chat_id, Path(path).name, Path(path).read_bytes(), caption)) or True
runner.send_message_sync = lambda chat_id, text, **kw: sent_texts.append((chat_id, text)) or True
backup.TELEGRAM_PART_BYTES = 1000
result = backup.send_backup_to_telegram(big)
check("every admin gets every part, in order", (result, [(c, n[-10:]) for c, n, _d, _cap in sent_files]),
      ((2, 2), [(11, "part01of03"), (11, "part02of03"), (11, "part03of03"), (22, "part01of03"), (22, "part02of03"), (22, "part03of03")]))
check("what one admin received joins back into the backup",
      backup.join_parts([(n, d) for c, n, d, _cap in sent_files if c == 11]) == payload, True)
check("captions say which piece; one explanation per admin", ("تکه 2 از 3" in sent_files[1][3], [c for c, _t in sent_texts]), (True, [11, 22]))
check("the temporary pieces are removed afterwards", [p.name for p in folder.iterdir()], [big.name])
sent_files.clear(); sent_texts.clear()
backup.TELEGRAM_PART_BYTES = 10_000
check("a small backup: one file, no explanation", (backup.send_backup_to_telegram(big), len(sent_files), sent_texts), ((2, 2), 2, []))
attempts = []
sent_texts.clear()


def flaky(chat_id, path, caption="", **kw):
    attempts.append((chat_id, Path(path).name[-10:], kw.get("timeout")))
    if chat_id == 22 and "part01" in path:
        return False                                    # the big first piece never gets through for this admin
    if chat_id == 11 and "part02" in path:
        return len([a for a in attempts if a[:2] == (11, "part02of03")]) >= 2      # second try works
    return True


runner.send_document_sync = flaky
backup.TELEGRAM_PART_BYTES = 1000
result = backup.send_backup_to_telegram(big)
check("a piece that fails is retried; an admin who still misses one is not counted as sent", result, (1, 2))
check("the failing piece was tried three times, the flaky one twice, each with a timeout",
      (len([a for a in attempts if a[:2] == (22, "part01of03")]), len([a for a in attempts if a[:2] == (11, "part02of03")]),
       all(a[2] and a[2] >= 90 for a in attempts)), (3, 2, True))
check("the admin with a missing piece is TOLD the set is incomplete; the other gets the normal explanation",
      ([("کامل نرسید" in t, "تکه‌(های) 1 از 3" in t) for c, t in sent_texts if c == 22],
       ["بازگردانی" in t and "کامل نرسید" not in t for c, t in sent_texts if c == 11]), ([(True, True)], [True]))

print("--- the backup copy is compacted, the live database is not touched ---")
live = folder / "live.db"
conn = sqlite3.connect(live)
conn.execute("CREATE TABLE t (x BLOB)")
conn.executemany("INSERT INTO t VALUES (?)", [(os.urandom(4000),) for _ in range(300)])
conn.commit()
conn.execute("DELETE FROM t WHERE rowid > 5")
conn.commit()
conn.close()
live_size = live.stat().st_size
backup.BACKUP_DIR = folder / "out"
backup._db_path = lambda: str(live)
backup._is_mysql = lambda: False
made = backup.create_backup()
restored = gzip.decompress(made.read_bytes())
check("the backup is a valid database with the surviving rows",
      (restored[:15], sqlite3.connect(str(live)).execute("SELECT COUNT(*) FROM t").fetchone()[0]), (b"SQLite format 3", 5))
check("deleted rows' pages are not carried into the backup; the live file is unchanged",
      (len(restored) < live_size / 5, live.stat().st_size == live_size), (True, True))

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
