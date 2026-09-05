"""Force-approve pending infra devices (switch/ap/router/gateway/firewall).

Unlike bulk-approve, this skips the canvas-node check and directly sets
status=approved. Use when infra devices are stuck as pending because they
already have canvas nodes from a prior approval run.

Usage (via Makefile): make force-approve-infra
Authenticates via MCP service key (MCP_SERVICE_KEY from env).
"""
import asyncio
import os
import sys

import httpx

BASE = "http://localhost:8000/api/v1"
INFRA_TYPES = {"switch", "ap", "router", "gateway", "firewall"}


async def main() -> None:
    service_key = os.environ.get("MCP_SERVICE_KEY", "")
    if not service_key:
        print("  ERROR: MCP_SERVICE_KEY not set", file=sys.stderr)
        sys.exit(1)

    headers = {"X-Mcp-Service-Key": service_key}

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/scan/pending", headers=headers)
        r.raise_for_status()
        devices = r.json()

        targets = [
            d for d in devices
            if d.get("status") == "pending"
            and (d.get("suggested_type") or d.get("type") or "").lower() in INFRA_TYPES
        ]

        if not targets:
            print("  No pending infra devices found")
            return

        print(f"  Force-approving {len(targets)} pending infra device(s):")
        updated = 0
        for dev in targets:
            label = dev.get("label") or dev.get("hostname") or dev["id"]
            stype = dev.get("suggested_type") or dev.get("type") or "?"
            r = await c.patch(
                f"{BASE}/scan/pending/{dev['id']}",
                headers=headers,
                json={"status": "approved"},
            )
            if r.status_code == 200:
                print(f"    approved: {label} ({stype})")
                updated += 1
            else:
                print(f"    FAILED:   {label} — {r.status_code} {r.text[:80]}")

        print(f"  Done — {updated}/{len(targets)} approved")


asyncio.run(main())
