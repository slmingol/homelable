"""Trigger an immediate network scan via the backend API.

Usage: make scan-now
"""
import asyncio
import os
import sys

import httpx

BASE = "http://localhost:8000/api/v1"


async def main() -> None:
    service_key = os.environ.get("MCP_SERVICE_KEY", "")
    if not service_key:
        print("  ERROR: MCP_SERVICE_KEY not set", file=sys.stderr)
        sys.exit(1)

    headers = {"X-Mcp-Service-Key": service_key}

    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{BASE}/scan/trigger", headers=headers)
        if r.status_code in (200, 201):
            run = r.json()
            print(f"  Scan queued — run id: {run.get('id')} status: {run.get('status')}")
        else:
            print(f"  ERROR: {r.status_code} {r.text[:200]}", file=sys.stderr)
            sys.exit(1)


asyncio.run(main())
