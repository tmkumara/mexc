#!/usr/bin/env python3
"""
Utility: wipe all signal records from the database.

Usage:
    python clear_db.py          # prompts for confirmation
    python clear_db.py --yes    # skip prompt (for automation)
"""

import sys
import sqlite3
from config import DB_PATH


def clear(skip_confirm: bool = False) -> None:
    if not skip_confirm:
        ans = input(f"Delete ALL records from '{DB_PATH}'? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted.")
            return

    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("DELETE FROM signals")
        con.execute("DELETE FROM sqlite_sequence WHERE name='signals'")
        con.commit()
        print(f"Cleared {cur.rowcount} signal(s). Auto-increment reset.")


if __name__ == "__main__":
    clear(skip_confirm="--yes" in sys.argv)
