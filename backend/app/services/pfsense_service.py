"""pfSense REST API client (pfsense-api package by jaredhendrickson13).

Auth: Authorization header with the API key.
Fetches ARP table and DHCP leases to build a device inventory.
Supports both v1 and v2 API paths.
"""
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": api_key}


async def test_pfsense_connection(
    host: str,
    port: int,
    api_key: str,
    scheme: str = "https",
    verify_tls: bool = False,
) -> tuple[bool, str]:
    base = f"{scheme}://{host}:{port}"
    headers = _auth_headers(api_key)
    try:
        async with httpx.AsyncClient(verify=verify_tls, timeout=10.0) as client:
            for path in ["/api/v1/diagnostics/arp", "/api/v2/diagnostics/arp-table"]:
                try:
                    r = await client.get(f"{base}{path}", headers=headers)
                    if r.status_code == 401:
                        return False, "Authentication failed: invalid API key"
                    if r.status_code == 403:
                        return False, "API key lacks read permission for ARP table"
                    if r.status_code == 200:
                        data = r.json()
                        entries = _extract_list(data)
                        return True, f"Connected — {len(entries)} ARP entr{'y' if len(entries) == 1 else 'ies'} found"
                except httpx.HTTPStatusError:
                    continue
            return False, f"No supported pfSense API found at {host}:{port}"
    except httpx.ConnectError as exc:
        return False, f"Cannot reach {host}:{port} — {exc}"
    except Exception as exc:
        return False, str(exc)


async def fetch_pfsense_inventory(
    host: str,
    port: int,
    api_key: str,
    scheme: str = "https",
    verify_tls: bool = False,
) -> list[dict[str, Any]]:
    """Fetch ARP table + DHCP leases and return normalized device dicts."""
    base = f"{scheme}://{host}:{port}"
    headers = _auth_headers(api_key)

    async with httpx.AsyncClient(verify=verify_tls, timeout=15.0) as client:
        arp = await _fetch_arp(client, base, headers)
        leases = await _fetch_dhcp_leases(client, base, headers)

    lease_by_mac: dict[str, dict[str, Any]] = {}
    for lease in leases:
        mac = _norm_mac(lease.get("mac") or lease.get("if_phys_addr") or "")
        if mac:
            lease_by_mac[mac] = lease

    seen_macs: set[str] = set()
    results: list[dict[str, Any]] = []

    for entry in arp:
        mac = _norm_mac(entry.get("mac") or entry.get("mac-address") or "")
        ip = (entry.get("ip") or entry.get("ip-address") or "").strip()
        if not mac and not ip:
            continue
        if mac in seen_macs:
            continue
        if mac:
            seen_macs.add(mac)

        lease = lease_by_mac.get(mac, {})
        hostname = (
            entry.get("hostname")
            or lease.get("hostname")
            or lease.get("descr")
            or None
        )
        if hostname:
            hostname = hostname.strip() or None

        iface = (entry.get("interface") or entry.get("intf") or "").strip()
        props: list[dict[str, str]] = []
        if iface:
            props.append({"name": "Interface", "value": iface})
        lease_state = lease.get("type") or lease.get("state")
        if lease_state:
            props.append({"name": "DHCP type", "value": str(lease_state)})

        ieee = f"pfsense-{mac}" if mac else f"pfsense-{ip}"
        results.append({
            "ieee_address": ieee,
            "mac": mac or None,
            "ip": ip or None,
            "hostname": hostname,
            "label": hostname or mac or ip,
            "type": "device",
            "vendor": None,
            "model": None,
            "properties": props,
        })

    # DHCP-only entries (static/offline)
    for mac, lease in lease_by_mac.items():
        if mac in seen_macs:
            continue
        ip = (lease.get("ip") or lease.get("ipaddr") or "").strip()
        hostname = (lease.get("hostname") or lease.get("descr") or "").strip() or None
        ieee = f"pfsense-{mac}" if mac else f"pfsense-{ip}"
        results.append({
            "ieee_address": ieee,
            "mac": mac or None,
            "ip": ip or None,
            "hostname": hostname,
            "label": hostname or mac or ip,
            "type": "device",
            "vendor": None,
            "model": None,
            "properties": [{"name": "DHCP type", "value": "static"}],
        })
        seen_macs.add(mac)

    logger.info("pfSense: %d devices fetched from %s", len(results), host)
    return results


def _norm_mac(raw: str) -> str:
    return raw.lower().strip()


def _extract_list(data: dict[str, Any]) -> list[Any]:
    """Handle both v1 ({data: [...]}) and v2 ({data: {arp_table: [...]}}) responses."""
    if isinstance(data.get("data"), list):
        return data["data"]
    if isinstance(data.get("data"), dict):
        for key in ("arp_table", "arp", "rows"):
            if isinstance(data["data"].get(key), list):
                return data["data"][key]
    return []


async def _fetch_arp(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    for path in ["/api/v1/diagnostics/arp", "/api/v2/diagnostics/arp-table"]:
        try:
            r = await client.get(f"{base}{path}", headers=headers)
            if r.status_code == 200:
                return _extract_list(r.json())
        except Exception:
            continue
    logger.warning("pfSense: ARP fetch failed on all known paths")
    return []


async def _fetch_dhcp_leases(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    for path in ["/api/v1/services/dhcpd/lease", "/api/v2/services/dhcp-server/leases"]:
        try:
            r = await client.get(f"{base}{path}", headers=headers)
            if r.status_code == 200:
                return _extract_list(r.json())
        except Exception:
            continue
    logger.debug("pfSense: DHCP lease fetch failed (may not be available)")
    return []
