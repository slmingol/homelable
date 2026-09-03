"""Approve all pending inventory devices from a given discovery source.

Usage (via Makefile): make approve-source SOURCE=pfsense
Authenticates via MCP service key (MCP_SERVICE_KEY from env).
"""
import asyncio
import os
import sys

import httpx

BASE = "http://localhost:8000/api/v1"


async def main(source: str) -> None:
    service_key = os.environ.get("MCP_SERVICE_KEY", "")
    if not service_key:
        print("  ERROR: MCP_SERVICE_KEY not set", file=sys.stderr)
        sys.exit(1)

    headers = {"X-Mcp-Service-Key": service_key}

    async with httpx.AsyncClient(timeout=30) as c:

        r = await c.get(f"{BASE}/scan/pending", headers=headers)
        r.raise_for_status()
        devices = r.json()

        ids = [d["id"] for d in devices
               if d.get("status") == "pending"
               and source in (d.get("discovery_sources") or [d.get("discovery_source", "")])]

        if not ids:
            print(f"  No pending devices from source '{source}'")
            return

        print(f"  Approving {len(ids)} pending device(s) from '{source}'...")
        r = await c.post(f"{BASE}/scan/pending/bulk-approve", headers=headers,
                         json={"device_ids": ids, "design_id": None})
        r.raise_for_status()
        result = r.json()
        placed = result.get("placed", 0)
        skipped = result.get("skipped", 0)
        print(f"  Done — {placed} placed, {skipped} skipped (already on canvas)")

source = sys.argv[1] if len(sys.argv) > 1 else "pfsense"
asyncio.run(main(source))
