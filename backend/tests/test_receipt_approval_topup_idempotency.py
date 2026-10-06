"""Permanent acceptance regression for the A2 wallet-top-up invariant.

The shared harness exercises SQLite and the isolated MariaDB scenario used by CI.
"""
from __future__ import annotations

import sys
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR))

import repro_topup_idempotency


if __name__ == "__main__":
    raise SystemExit(repro_topup_idempotency.main())
