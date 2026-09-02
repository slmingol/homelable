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
    url    = os.environ.get("OPNSENSE_URL", "")
    key    = os.environ.get("OPNSENSE_API_KEY", "")
    secret = os.environ.get("OPNSENSE_API_SECRET", "")
    if not url:
        print("  OPNsense: OPNSENSE_URL not set — skipped")
        return
    base  = url.rstrip("/")
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    async with httpx.AsyncClient(verify=False, timeout=5) as c:
        try:
            r = await c.get(
                f"{base}/api/diagnostics/interface/getArp",
                headers={"Authorization": f"Basic {token}"},
            )
            if r.status_code == 200:
                rows = r.json().get("rows", [])
                print(f"  OPNsense: connected — {len(rows)} ARP entries")
            else:
                print(f"  OPNsense: HTTP {r.status_code}: {r.text[:80]}")
        except Exception as e:
            print(f"  OPNsense: {repr(e)}")


async def test_pfsense():
    url = os.environ.get("PFSENSE_URL", "")
    key = os.environ.get("PFSENSE_API_KEY", "")
    if not url:
        print("  pfSense : PFSENSE_URL not set — skipped")
        return
    base = url.rstrip("/")
    async with httpx.AsyncClient(verify=False, timeout=10) as c:
        for path in ["/api/v1/diagnostics/arp", "/api/v2/diagnostics/arp-table"]:
            try:
                r = await c.get(f"{base}{path}", headers={"Authorization": key})
                if r.status_code == 200:
                    data = r.json()
                    rows = data.get("data", []) if isinstance(data.get("data"), list) else []
                    print(f"  pfSense : connected via {path} — {len(rows)} ARP entries")
                    return
                print(f"  pfSense : {path} → HTTP {r.status_code}: {r.text[:120]}")
            except Exception as e:
                print(f"  pfSense : {path} → {repr(e)}")
    print("  pfSense : no supported API path succeeded")


async def main():
    await asyncio.gather(test_unifi(), test_opnsense(), test_pfsense())

asyncio.run(main())
