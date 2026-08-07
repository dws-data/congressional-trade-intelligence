# db.py
# Single source of truth for connecting to trades.db.
#
# Added 2026-08-07 (remote-db-hosting-brief.md, step 3.5) — before this,
# every one of ~30 files defined its own DB_PATH constant and called
# sqlite3.connect() independently. Only 4 of ~59 call sites had picked up
# the timeout=30 fix from the 2026-08-05 "database is locked" debugging;
# the rest were still on SQLite's 5s default, same latent exposure.
# Centralizing here closes that gap everywhere at once, and gives the
# planned Turso/libsql migration (step 5) a single function to extend
# instead of 30 files to edit.
#
# Usage:
#   from db import get_connection
#   conn = get_connection()                      # trades.db, default timeout
#   conn = get_connection(db_path)                # override, e.g. fmp_staging.db
#
# Run from anywhere (direct script, `python -m pipeline.x`, or imported by
# daily_runner.py) — this file lives at the project root, so callers that
# aren't already on sys.path should insert ROOT first:
#   import sys
#   from pathlib import Path
#   sys.path.insert(0, str(Path(__file__).parent.parent))
#   from db import get_connection

import sqlite3
from pathlib import Path

ROOT    = Path(__file__).parent
DB_PATH = ROOT / "data" / "trades.db"

DEFAULT_TIMEOUT = 30   # was SQLite's 5s default pre-2026-08-05; bumped
                        # because commits against a multi-GB file under
                        # concurrent readers can take longer than that.


def get_connection(db_path=None, timeout=DEFAULT_TIMEOUT):
    """
    Return a sqlite3 connection to trades.db, or to db_path if given
    (e.g. data/fmp_staging.db for the staging pipeline).
    """
    path = str(db_path) if db_path else str(DB_PATH)
    return sqlite3.connect(path, timeout=timeout)
