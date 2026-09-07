"""pfSense REST API client (pfrest/pfSense-pkg-RESTAPI).

Auth: x-api-key header with the API key.
ARP table via diagnostics/command_prompt (arp -an).
DHCP static mappings via /api/v2/services/dhcp_servers.
"""
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ARP_RE = re.compile(
    r"\?\s+\((?P<ip>\d+\.\d+\.\d+\.\d+)\)\s+at\s+(?P<mac>[0-9a-f:]+)\s+on\s+(?P<intf>\S+)",
    re.IGNORECASE,
)


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key}


async def test_pfsense_connection(
    base_url: str,
    api_key: str,
    verify_tls: bool = False,
) -> tuple[bool, str]:
    base = base_url.rstrip("/")
    headers = _auth_headers(api_key)
    try:
        async with httpx.AsyncClient(verify=verify_tls, timeout=10.0) as client:
            r = await client.post(
                f"{base}/api/v2/diagnostics/command_prompt",
                headers=headers,
                json={"command": "arp -an 2>/dev/null | head -5"},
            )
            if r.status_code == 401:
                return False, "Authentication failed: invalid API key"
            if r.status_code == 403:
                return False, "API key lacks permission (check pfSense API settings)"
            if r.status_code == 200:
                output = (r.json().get("data") or {}).get("output", "")
                entries = _parse_arp_output(output)
                return True, f"Connected — {len(entries)} ARP entries sampled (full sync will fetch all)"
            return False, f"Unexpected response {r.status_code} from {base_url}"
    except httpx.ConnectError as exc:
        return False, f"Cannot reach {base_url} — {exc}"
    except Exception as exc:
        return False, str(exc)


async def fetch_pfsense_inventory(
    base_url: str,
    api_key: str,
    verify_tls: bool = False,
) -> list[dict[str, Any]]:
    """Fetch ARP table + DHCP static mappings and return normalized device dicts."""
    base = base_url.rstrip("/")
    headers = _auth_headers(api_key)

    async with httpx.AsyncClient(verify=verify_tls, timeout=15.0) as client:
        arp = await _fetch_arp(client, base, headers)
        static_leases = await _fetch_dhcp_static(client, base, headers)

    lease_by_mac: dict[str, dict[str, Any]] = {
        _norm_mac(m.get("mac", "")): m
        for m in static_leases
        if m.get("mac")
    }

    seen_macs: set[str] = set()
    results: list[dict[str, Any]] = []

    for entry in arp:
        mac = _norm_mac(entry.get("mac", ""))
        ip = entry.get("ip", "").strip()
        if not mac and not ip:
            continue
        if mac in seen_macs:
            continue
        if mac:
            seen_macs.add(mac)

        lease = lease_by_mac.get(mac, {})
        hostname = (lease.get("hostname") or lease.get("descr") or "").strip() or None
        iface = entry.get("intf", "").strip()
        props: list[dict[str, str]] = []
        if iface:
            props.append({"name": "Interface", "value": iface})

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

    # Static DHCP entries not in ARP (offline hosts)
    for mac, lease in lease_by_mac.items():
        if mac in seen_macs:
            continue
        ip = (lease.get("ipaddr") or lease.get("ip") or "").strip()
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

    logger.info("pfSense: %d devices fetched from %s", len(results), base_url)
    return results


def _norm_mac(raw: str) -> str:
    return raw.lower().strip()


def _parse_arp_output(output: str) -> list[dict[str, str]]:
    results = []
    for line in output.splitlines():
        m = _ARP_RE.search(line)
        if m:
            results.append({"ip": m.group("ip"), "mac": m.group("mac"), "intf": m.group("intf")})
    return results


async def _fetch_arp(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
) -> list[dict[str, str]]:
    try:
        r = await client.post(
            f"{base}/api/v2/diagnostics/command_prompt",
            headers=headers,
            json={"command": "arp -an 2>/dev/null"},
        )
        if r.status_code == 200:
            output = (r.json().get("data") or {}).get("output", "")
            return _parse_arp_output(output)
        logger.warning("pfSense ARP command failed: HTTP %s", r.status_code)
    except Exception as exc:
        logger.warning("pfSense ARP fetch failed: %s", exc)
    return []


async def fetch_dhcp_hostname_macs(
    base_url: str,
    api_key: str,
    verify_tls: bool = False,
) -> dict[str, str]:
    """Return {lease_mac → hostname} for all active DHCP leases (dynamic + static).

    Tries the REST leases endpoint first; falls back to parsing the raw
    dhcpd.leases file via the command_prompt API so randomized-MAC devices
    (dynamic leases) are included alongside static mappings.
    """
    base = base_url.rstrip("/")
    headers = _auth_headers(api_key)
    out: dict[str, str] = {}

    async with httpx.AsyncClient(verify=verify_tls, timeout=15.0) as client:
        # 1. Try REST leases endpoint (pfSense-pkg-RESTAPI >= 2.x)
        try:
            r = await client.get(
                f"{base}/api/v2/services/dhcp_server/leases",
                headers=headers,
                params={"limit": 0},
            )
            if r.status_code == 200:
                for lease in (r.json().get("data") or []):
                    mac = _norm_mac(lease.get("mac") or lease.get("hw-mac") or "")
                    hostname = (lease.get("hostname") or lease.get("client-hostname") or "").strip()
                    if mac and hostname:
                        out[mac] = hostname
                logger.info("pfSense: %d dynamic lease aliases from REST endpoint", len(out))
                # Also pull static mappings
                for m in await _fetch_dhcp_static(client, base, headers):
                    mac = _norm_mac(m.get("mac") or "")
                    hostname = (m.get("hostname") or m.get("descr") or "").strip()
                    if mac and hostname and mac not in out:
                        out[mac] = hostname
                return out
        except Exception:
            pass

        # 2. Fall back: parse /var/dhcpd/var/db/dhcpd.leases via command_prompt
        try:
            r = await client.post(
                f"{base}/api/v2/diagnostics/command_prompt",
                headers=headers,
                json={"command": "cat /var/dhcpd/var/db/dhcpd.leases 2>/dev/null"},
            )
            if r.status_code == 200:
                raw = (r.json().get("data") or {}).get("output", "")
                out.update(_parse_dhcpd_leases(raw))
                logger.info("pfSense: %d lease aliases from dhcpd.leases file", len(out))
        except Exception as exc:
            logger.warning("pfSense DHCP lease fetch failed: %s", exc)

        # Always include static mappings
        for m in await _fetch_dhcp_static(client, base, headers):
            mac = _norm_mac(m.get("mac") or "")
            hostname = (m.get("hostname") or m.get("descr") or "").strip()
            if mac and hostname and mac not in out:
                out[mac] = hostname

    return out


def _parse_dhcpd_leases(raw: str) -> dict[str, str]:
    """Parse ISC dhcpd lease file → {mac: hostname} for active/free leases."""
    out: dict[str, str] = {}
    mac = hostname = ""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("lease "):
            mac = hostname = ""
        elif line.startswith("hardware ethernet"):
            mac = _norm_mac(line.split()[-1].rstrip(";"))
        elif line.startswith("client-hostname"):
            hostname = line.split(None, 1)[-1].strip().strip('";')
        elif line == "}":
            if mac and hostname:
                out[mac] = hostname
            mac = hostname = ""
    return out


async def _fetch_dhcp_static(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    try:
        r = await client.get(
            f"{base}/api/v2/services/dhcp_servers",
            headers=headers,
            params={"limit": 0},
        )
        if r.status_code == 200:
            servers = r.json().get("data") or []
            mappings = []
            for server in servers:
                mappings.extend(server.get("staticmap") or [])
            return mappings
        logger.debug("pfSense DHCP static fetch: HTTP %s", r.status_code)
    except Exception as exc:
        logger.debug("pfSense DHCP static fetch failed: %s", exc)
    return []
