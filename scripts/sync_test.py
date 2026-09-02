import asyncio
import base64
import os

import httpx


async def test_unifi():
    url  = os.environ.get("UNIFI_URL", "")
    user = os.environ.get("UNIFI_USER", os.environ.get("UNIFI_USERNAME", ""))
    pw   = os.environ.get("UNIFI_PASS", os.environ.get("UNIFI_PASSWORD", ""))
    if not url:
        print("  UniFi   : UNIFI_URL not set — skipped")
        return
    base = url.rstrip("/")
    async with httpx.AsyncClient(verify=False, timeout=5) as c:
        for path in ["/api/auth/login", "/api/login"]:
            try:
                r = await c.post(f"{base}{path}", json={"username": user, "password": pw})
                if r.status_code in (200, 201):
                    print(f"  UniFi   : connected via {path}")
                    return
            except Exception:
                continue
    print("  UniFi   : login failed")


async def test_opnsense():
    host   = os.environ.get("OPNSENSE_HOST", "")
    key    = os.environ.get("OPNSENSE_API_KEY", "")
    secret = os.environ.get("OPNSENSE_API_SECRET", "")
    if not host:
        print("  OPNsense: OPNSENSE_HOST not set — skipped")
        return
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    async with httpx.AsyncClient(verify=False, timeout=5) as c:
        try:
            r = await c.get(
                f"https://{host}/api/diagnostics/interface/getArp",
                headers={"Authorization": f"Basic {token}"},
            )
            if r.status_code == 200:
                rows = r.json().get("rows", [])
                print(f"  OPNsense: connected — {len(rows)} ARP entries")
            else:
                print(f"  OPNsense: HTTP {r.status_code}")
        except Exception as e:
            print(f"  OPNsense: {e}")


async def test_pfsense():
    host = os.environ.get("PFSENSE_HOST", "")
    key  = os.environ.get("PFSENSE_API_KEY", "")
    if not host:
        print("  pfSense : PFSENSE_HOST not set — skipped")
        return
    async with httpx.AsyncClient(verify=False, timeout=5) as c:
        for path in ["/api/v1/diagnostics/arp", "/api/v2/diagnostics/arp-table"]:
            try:
                r = await c.get(f"https://{host}{path}", headers={"Authorization": key})
                if r.status_code == 200:
                    data = r.json()
                    rows = data.get("data", []) if isinstance(data.get("data"), list) else []
                    print(f"  pfSense : connected via {path} — {len(rows)} ARP entries")
                    return
            except Exception:
                continue
    print("  pfSense : no supported API responded")


async def main():
    await asyncio.gather(test_unifi(), test_opnsense(), test_pfsense())

asyncio.run(main())
