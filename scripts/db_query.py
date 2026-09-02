import sqlite3
import os
import sys

db = os.environ.get("DB_PATH", "/app/data/homelab.db")
sql = os.environ.get("SQL", "").strip()

if not sql:
    print("Usage: make db-query SQL=\"SELECT ...\"")
    sys.exit(1)

c = sqlite3.connect(db)
try:
    rows = c.execute(sql).fetchall()
    for r in rows:
        print(r)
    print(f"\n  {len(rows)} row(s)")
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)
