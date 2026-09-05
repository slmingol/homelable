"""XCP-ng / XenServer inventory service: fetch hosts + VMs via the XAPI XML-RPC interface.

Talks to the XAPI endpoint (https://<host>/) using standard Python xmlrpc.client.
Authentication: session.login_with_password (username + password).
All blocking calls are wrapped in asyncio.to_thread so the async FastAPI event loop
is never blocked.

XAPI response envelope: {'Status': 'Success'|'Failure', 'Value': ..., 'ErrorDescription': [...]}
"""

from __future__ import annotations

import asyncio
import http.client
import logging
import ssl
import xmlrpc.client
from typing import Any

from app.services.mac_utils import normalize_mac
from app.services.zigbee_service import merge_zigbee_properties

logger = logging.getLogger(__name__)

merge_xcpng_properties = merge_zigbee_properties

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0
_NULL_REF = "OpaqueRef:NULL"
_BYTES_PER_GB = 1024 ** 3


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    """SafeTransport with a per-connection timeout."""

    def __init__(self, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(context=context)
        self._timeout = timeout

    def make_connection(self, host: Any) -> http.client.HTTPSConnection:
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def _make_proxy(host: str, verify_tls: bool) -> xmlrpc.client.ServerProxy:
    ctx = ssl.create_default_context()
    if not verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    transport = _TimeoutSafeTransport(timeout=_READ_TIMEOUT, context=ctx)
    return xmlrpc.client.ServerProxy(f"https://{host}/", transport=transport)


def _xapi(result: Any, method: str) -> Any:
    """Unwrap an XAPI response dict; raise on Failure."""
    if not isinstance(result, dict):
        raise ValueError(f"XAPI {method}: unexpected response type {type(result)}")
    if result.get("Status") != "Success":
        err = result.get("ErrorDescription", ["unknown error"])
        raise ConnectionError(f"XAPI {method} failed: {err}")
    return result["Value"]


def _gb(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return round(n / _BYTES_PER_GB, 1)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sanitize_error(exc: BaseException) -> str:
    logger.warning("XCP-ng XAPI error (sanitized): %r", exc)
    raw = str(exc).lower()
    if "session_authentication_failed" in raw or "unauthorized" in raw:
        return "Authentication failed — check XCPNG_USERNAME and XCPNG_PASSWORD"
    if "certificate" in raw or "ssl" in raw or "tls" in raw:
        return "TLS verification failed — set XCPNG_VERIFY_TLS=false for self-signed certs"
    if "timed out" in raw or "timeout" in raw:
        return "Connection to XCP-ng host timed out"
    if "refused" in raw:
        return "Connection refused by XCP-ng host"
    if "nodename nor servname" in raw or "getaddrinfo" in raw or "name or service not known" in raw:
        return "XCP-ng host could not be resolved"
    if "errorcode" in raw and "session" in raw:
        return "Authentication failed — check XCPNG_USERNAME and XCPNG_PASSWORD"
    return f"XCP-ng XAPI connection failed: {type(exc).__name__}"


def _host_node(uuid: str, rec: dict[str, Any]) -> dict[str, Any] | None:
    name = rec.get("name_label") or rec.get("hostname") or uuid
    cpu_info = rec.get("cpu_info") or {}
    try:
        cpu_count = int(cpu_info.get("cpu_count") or 0) or None
    except (TypeError, ValueError):
        cpu_count = None
    return {
        "id": f"xcpng-host-{uuid}",
        "label": name,
        "type": "xcpng",
        "ieee_address": f"xcpng-host-{uuid}",
        "hostname": name,
        "ip": rec.get("address") or None,
        "status": "online",
        "cpu_count": cpu_count,
        "ram_gb": _gb(rec.get("memory_total")),
        "disk_gb": None,
        "vendor": "XCP-ng",
        "model": rec.get("software_version", {}).get("product_version") or "Hypervisor",
        "parent_ieee": None,
    }


def _vm_node(
    uuid: str,
    rec: dict[str, Any],
    host_ieee: str | None,
    mac: str | None,
) -> dict[str, Any] | None:
    name = rec.get("name_label") or f"vm-{uuid}"
    power = rec.get("power_state", "Halted")
    return {
        "id": f"xcpng-vm-{uuid}",
        "label": name,
        "type": "vm",
        "ieee_address": f"xcpng-vm-{uuid}",
        "hostname": name,
        "ip": None,
        "mac": mac,
        "status": "online" if power == "Running" else "offline",
        "cpu_count": _int_or_none(rec.get("VCPUs_max")),
        "ram_gb": _gb(rec.get("memory_static_max")),
        "disk_gb": None,
        "vendor": "XCP-ng",
        "model": "VM",
        "vmid": uuid,
        "parent_ieee": host_ieee,
    }


def _parse_inventory(
    hosts_raw: dict[str, dict[str, Any]],
    vms_raw: dict[str, dict[str, Any]],
    vifs_raw: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Build host uuid → ieee map
    host_ieee: dict[str, str] = {}
    for ref, rec in hosts_raw.items():
        uuid = rec.get("uuid")
        if not uuid:
            continue
        node = _host_node(uuid, rec)
        if node and node["id"] not in seen:
            seen.add(node["id"])
            nodes.append(node)
            host_ieee[ref] = node["ieee_address"]

    # Build vm_ref → first MAC from VIFs
    vm_mac: dict[str, str | None] = {}
    for _ref, vif in vifs_raw.items():
        vm_ref = vif.get("VM")
        if not vm_ref or vm_ref in vm_mac:
            continue
        raw_mac = vif.get("MAC") or ""
        vm_mac[vm_ref] = normalize_mac(raw_mac) if raw_mac else None

    # Build VMs
    for ref, rec in vms_raw.items():
        if rec.get("is_a_template") or rec.get("is_control_domain"):
            continue
        uuid = rec.get("uuid")
        if not uuid:
            continue

        resident = rec.get("resident_on") or _NULL_REF
        parent_ieee = host_ieee.get(resident) if resident != _NULL_REF else None
        if parent_ieee is None and len(host_ieee) == 1:
            # Single-host setup — affinity to the only host regardless of power state
            parent_ieee = next(iter(host_ieee.values()))

        mac = vm_mac.get(ref)
        node = _vm_node(uuid, rec, parent_ieee, mac)
        if node and node["id"] not in seen:
            seen.add(node["id"])
            nodes.append(node)
            if parent_ieee:
                edges.append({"source": parent_ieee, "target": node["ieee_address"]})

    return nodes, edges


def _fetch_inventory_sync(
    host: str,
    username: str,
    password: str,
    verify_tls: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    proxy = _make_proxy(host, verify_tls)
    session = _xapi(
        proxy.session.login_with_password(username, password, "1.3", "homelable"),
        "session.login_with_password",
    )
    try:
        hosts_raw = _xapi(proxy.host.get_all_records(session), "host.get_all_records")
        vms_raw = _xapi(proxy.VM.get_all_records(session), "VM.get_all_records")
        vifs_raw = _xapi(proxy.VIF.get_all_records(session), "VIF.get_all_records")
    finally:
        try:
            proxy.session.logout(session)
        except Exception:
            pass
    return _parse_inventory(hosts_raw, vms_raw, vifs_raw)


def _test_connection_sync(
    host: str,
    username: str,
    password: str,
    verify_tls: bool,
) -> tuple[bool, str]:
    proxy = _make_proxy(host, verify_tls)
    try:
        session = _xapi(
            proxy.session.login_with_password(username, password, "1.3", "homelable"),
            "session.login_with_password",
        )
        try:
            result = proxy.host.get_all_records(session)
            hosts = _xapi(result, "host.get_all_records")
            n_hosts = len(hosts) if isinstance(hosts, dict) else 0
            result = proxy.VM.get_all_records(session)
            vms_all = _xapi(result, "VM.get_all_records")
            n_vms = sum(
                1 for rec in (vms_all or {}).values()
                if not rec.get("is_a_template") and not rec.get("is_control_domain")
            )
        finally:
            try:
                proxy.session.logout(session)
            except Exception:
                pass
        return True, f"Connected to XCP-ng — {n_hosts} host(s), {n_vms} VM(s)"
    except ConnectionError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, _sanitize_error(exc)


async def fetch_xcpng_inventory(
    host: str,
    username: str,
    password: str,
    verify_tls: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch hosts + VMs from XCP-ng via XAPI. Returns (nodes, edges).

    Raises:
        ConnectionError: transport / auth / XAPI failures (sanitized message).
        ValueError: malformed response.
    """
    try:
        return await asyncio.to_thread(_fetch_inventory_sync, host, username, password, verify_tls)
    except ConnectionError:
        raise
    except Exception as exc:
        raise ConnectionError(_sanitize_error(exc)) from exc


async def test_xcpng_connection(
    host: str,
    username: str,
    password: str,
    verify_tls: bool = False,
) -> tuple[bool, str]:
    """Reachability + auth check. Returns (connected, message). Never raises credentials outward."""
    try:
        return await asyncio.to_thread(_test_connection_sync, host, username, password, verify_tls)
    except Exception as exc:
        return False, _sanitize_error(exc)


def build_xcpng_properties(node: dict[str, Any]) -> list[dict[str, Any]]:
    props: list[dict[str, Any]] = []
    vmid = node.get("vmid")
    if vmid:
        props.append({"key": "UUID", "value": str(vmid), "icon": None, "visible": False})
    if node.get("model"):
        props.append({"key": "Kind", "value": node["model"], "icon": None, "visible": False})
    if node.get("cpu_count") is not None:
        props.append({"key": "CPU Cores", "value": str(node["cpu_count"]), "icon": "Cpu", "visible": False})
    if node.get("ram_gb") is not None:
        props.append({"key": "RAM", "value": f"{node['ram_gb']} GB", "icon": "MemoryStick", "visible": False})
    props.append({"key": "Source", "value": "XCP-ng", "icon": None, "visible": False})
    return props
