"""Enable or disable SNMP on all approved inventory devices.

Usage (via Makefile): make snmp-enable
                      make snmp-enable SNMP_ENABLED=false
Authenticates via MCP service key (MCP_SERVICE_KEY from env).
"""
import asyncio
import os
import sys

import httpx

BASE = "http://localhost:8000/api/v1"


async def main(enabled: bool) -> None:
    service_key = os.environ.get("MCP_SERVICE_KEY", "")
    if not service_key:
        print("  ERROR: MCP_SERVICE_KEY not set", file=sys.stderr)
        sys.exit(1)

    headers = {"X-Mcp-Service-Key": service_key}

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{BASE}/scan/bulk-snmp",
            headers=headers,
            json={"device_ids": [], "snmp_enabled": enabled},
        )
        r.raise_for_status()
        result = r.json()
        action = "enabled" if enabled else "disabled"
        print(f"  SNMP {action} on {result['updated']} approved device(s)")


arg = sys.argv[1].lower() if len(sys.argv) > 1 else "true"
asyncio.run(main(arg not in ("false", "0", "no")))
