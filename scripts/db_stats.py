import sqlite3
import os

db = os.environ.get("DB_PATH", "/app/data/homelab.db")
c = sqlite3.connect(db)

rows = c.execute(
    "SELECT status, discovery_source, COUNT(*) "
    "FROM device_inventory "
    "GROUP BY status, discovery_source "
    "ORDER BY status, discovery_source"
).fetchall()

print()
print(f"  {'STATUS':<12} {'SOURCE':<14} {'COUNT':>5}")
print(f"  {'-'*12} {'-'*14} {'-'*5}")
for status, source, count in rows:
    print(f"  {(status or '-'):<12} {(source or '-'):<14} {count:>5}")

total = c.execute("SELECT COUNT(*) FROM device_inventory").fetchone()[0]
print(f"  {'─'*33}")
print(f"  {'TOTAL':<27} {total:>5}")
print()
