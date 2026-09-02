"""UniFi Network Controller REST API client.

Cookie-based auth: POST /api/login, then /api/s/<site>/stat/device.
Supports both legacy UniFi Controller (port 8443) and UniFi OS (port 443).
"""
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Map UniFi device type codes to homelable node types
_TYPE_MAP: dict[str, str] = {
    "ugw": "router",    # UniFi Security Gateway
    "udm": "router",    # UniFi Dream Machine / Dream Router
    "usg": "router",
    "usw": "switch",    # UniFi Switch
    "uap": "ap",        # UniFi Access Point
    "uxg": "router",    # UniFi Express Gateway
}


async def test_unifi_connection(
    host: str,
    port: int,
    site: str,
    username: str,
    password: str,
    verify_tls: bool = False,
) -> tuple[bool, str]:
    """Test UniFi controller reachability and credentials."""
    try:
        client = httpx.AsyncClient(verify=verify_tls, timeout=10.0)
        try:
            cookies = await _login(client, host, port, username, password)
            if not cookies:
                return False, "Login failed: invalid credentials or unreachable host"
            devices = await _fetch_devices(client, host, port, site, cookies)
            return True, f"Connected — {len(devices)} device(s) found in site '{site}'"
        finally:
            await client.aclose()
    except httpx.ConnectError as exc:
        return False, f"Cannot reach {host}:{port} — {exc}"
    except Exception as exc:
        return False, str(exc)


async def fetch_unifi_inventory(
    host: str,
    port: int,
    site: str,
    username: str,
    password: str,
    verify_tls: bool = False,
) -> list[dict[str, Any]]:
    """Fetch all devices from the UniFi controller and return normalized dicts."""
    client = httpx.AsyncClient(verify=verify_tls, timeout=15.0)
    try:
        cookies = await _login(client, host, port, username, password)
        if not cookies:
            raise ConnectionError("UniFi login failed: invalid credentials or unreachable host")
        raw = await _fetch_devices(client, host, port, site, cookies)
        return [_normalize(d) for d in raw]
    finally:
        await client.aclose()


async def _login(
    client: httpx.AsyncClient,
    host: str,
    port: int,
    username: str,
    password: str,
) -> dict[str, str] | None:
    """Try both UniFi OS and legacy controller login paths."""
    base = f"https://{host}:{port}"
    payload = {"username": username, "password": password}

    # UniFi OS (Dream Machine series) uses /api/auth/login
    for path in ["/api/auth/login", "/api/login"]:
        try:
            r = await client.post(f"{base}{path}", json=payload, follow_redirects=True)
            if r.status_code in (200, 201):
                return dict(r.cookies)
        except Exception:
            continue
    return None


async def _fetch_devices(
    client: httpx.AsyncClient,
    host: str,
    port: int,
    site: str,
    cookies: dict[str, str],
) -> list[dict[str, Any]]:
    base = f"https://{host}:{port}"
    headers = {}
    # UniFi OS requires X-CSRF-Token header
    if "csrf_token" in cookies:
        headers["X-CSRF-Token"] = cookies["csrf_token"]

    # Try UniFi OS proxy path first, then legacy path
    for path in [
        f"/proxy/network/api/s/{site}/stat/device",
        f"/api/s/{site}/stat/device",
    ]:
        try:
            r = await client.get(
                f"{base}{path}",
                cookies=cookies,
                headers=headers,
                follow_redirects=True,
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("data", [])
        except Exception:
            continue
    return []


def _normalize(d: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw UniFi device record to a homelable-compatible dict."""
    raw_type = (d.get("type") or "").lower()
    node_type = _TYPE_MAP.get(raw_type, "device")

    mac = (d.get("mac") or "").lower()
    ip = d.get("ip") or None
    name = d.get("name") or d.get("hostname") or mac or "unknown"
    model = d.get("model") or None
    version = d.get("version") or None

    ieee = f"unifi-{mac}" if mac else f"unifi-{name}"

    props: list[dict[str, str]] = []
    if raw_type:
        props.append({"name": "UniFi type", "value": raw_type})
    if model:
        props.append({"name": "Model", "value": model})
    if version:
        props.append({"name": "Firmware", "value": version})
    uptime = d.get("uptime")
    if uptime is not None:
        props.append({"name": "Uptime (s)", "value": str(uptime)})

    return {
        "ieee_address": ieee,
        "mac": mac,
        "ip": ip,
        "hostname": name,
        "label": name,
        "type": node_type,
        "vendor": "Ubiquiti",
        "model": model,
        "properties": props,
        "raw_type": raw_type,
    }
