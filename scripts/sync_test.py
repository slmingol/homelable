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
                data = r.json()
                rows = data if isinstance(data, list) else data.get("rows", [])
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
        try:
            r = await c.post(
                f"{base}/api/v2/diagnostics/command_prompt",
                headers={"x-api-key": key},
                json={"command": "arp -an 2>/dev/null | wc -l"},
            )
            if r.status_code == 200:
                output = (r.json().get("data") or {}).get("output", "").strip()
                print(f"  pfSense : connected — ~{output} ARP entries (command_prompt)")
            else:
                print(f"  pfSense : HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"  pfSense : {repr(e)}")


async def main():
    await asyncio.gather(test_unifi(), test_opnsense(), test_pfsense())

asyncio.run(main())
