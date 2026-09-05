"""Approve and type firewall/router devices stuck as pending with type=server.

Looks for pending devices whose hostname matches known firewall/router patterns
(opnsense, pfsense, vyos, openwrt, unifi-gw, udm) and patches them to
type=firewall, status=approved so they appear at t0 in the auto-place layout.

Usage: make approve-firewalls
"""
import asyncio
import os
import sys

import httpx

BASE = "http://localhost:8000/api/v1"
FW_PATTERNS = ("opnsense", "pfsense", "vyos", "openwrt", "unifi-gw", "udm", "usg")


async def main() -> None:
    service_key = os.environ.get("MCP_SERVICE_KEY", "")
    if not service_key:
        print("  ERROR: MCP_SERVICE_KEY not set", file=sys.stderr)
        sys.exit(1)

    headers = {"X-Mcp-Service-Key": service_key}

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/scan/pending", headers=headers)
        r.raise_for_status()
        all_devs = r.json()  # returns all non-hidden devices

        targets = []
        for dev in all_devs:
            hn = (dev.get("hostname") or dev.get("label") or "").lower()
            dev_type = (dev.get("type") or "").lower()
            stype = (dev.get("suggested_type") or "").lower()
            if any(p in hn for p in FW_PATTERNS) and dev_type not in ("firewall", "router", "gateway"):
                targets.append(dev)

        # Deduplicate by id
        seen: set[str] = set()
        targets = [d for d in targets if not (d["id"] in seen or seen.add(d["id"]))]  # type: ignore[func-returns-value]

        if not targets:
            print("  No pending/mis-typed firewall devices found")
            return

        print(f"  Patching {len(targets)} firewall device(s):")
        for dev in targets:
            label = dev.get("label") or dev.get("hostname") or dev["id"]
            r = await c.patch(
                f"{BASE}/scan/pending/{dev['id']}",
                headers=headers,
                json={"type": "firewall", "suggested_type": "firewall", "status": "approved"},
            )
            if r.status_code == 200:
                print(f"    approved+retyped: {label}")
            else:
                print(f"    FAILED: {label} — {r.status_code} {r.text[:80]}")


asyncio.run(main())
