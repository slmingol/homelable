"""XCP-ng inventory via Xen Orchestra (XO) JSON-RPC HTTP API.

Connects to the XO server (xcpng-xo-01 / xcpng-xo-02) at ``/api/`` using the
JSON-RPC 2.0 protocol over HTTPS. Authentication is session-based (cookie);
credentials are the XO email + password (xoa_homelable@...).

This avoids direct XAPI XML-RPC (which requires root or AD subjects) in favour
of XO's own service account support.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.mac_utils import normalize_mac
from app.services.zigbee_service import merge_zigbee_properties

logger = logging.getLogger(__name__)

merge_xcpng_properties = merge_zigbee_properties

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0
_BYTES_PER_GB = 1024 ** 3

_RPC_ID = 1  # stateless, single-request id is fine


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
    logger.warning("XO API error (sanitized): %r", exc)
    raw = str(exc).lower()
    if "unauthorized" in raw or "authentication" in raw or "invalid credentials" in raw:
        return "Authentication failed — check XCPNG_USERNAME and XCPNG_PASSWORD"
    if "certificate" in raw or "ssl" in raw or "tls" in raw:
        return "TLS verification failed — set XCPNG_VERIFY_TLS=false for self-signed certs"
    if "timed out" in raw or "timeout" in raw:
        return "Connection to XO server timed out"
    if "refused" in raw:
        return "Connection refused by XO server"
    if "nodename nor servname" in raw or "getaddrinfo" in raw or "name or service not known" in raw:
        return "XO host could not be resolved"
    if "invalid_credentials" in raw or "incorrect" in raw:
        return "Authentication failed — check XCPNG_USERNAME and XCPNG_PASSWORD"
    return f"XO API connection failed: {type(exc).__name__}"


def _make_client(host: str, verify_tls: bool) -> httpx.AsyncClient:
    timeout = httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT)
    return httpx.AsyncClient(
        base_url=f"https://{host}",
        verify=verify_tls,
        timeout=timeout,
        follow_redirects=True,
    )


async def _rpc(
    client: httpx.AsyncClient,
    method: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """Send one JSON-RPC 2.0 call; raise ConnectionError on RPC-level errors."""
    payload = {"jsonrpc": "2.0", "id": _RPC_ID, "method": method, "params": params or {}}
    resp = await client.post("/api/", json=payload)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        err = data["error"]
        code = err.get("code") if isinstance(err, dict) else None
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        if code in (1, -32000) or "credential" in msg.lower() or "authentication" in msg.lower():
            raise ConnectionError("Authentication failed — check XCPNG_USERNAME and XCPNG_PASSWORD")
        raise ConnectionError(f"XO RPC {method} error: {msg}")
    return data.get("result")


async def _sign_in(client: httpx.AsyncClient, username: str, password: str) -> None:
    """Establish a session; raises ConnectionError on auth failure."""
    await _rpc(client, "session.signIn", {"email": username, "password": password})


async def _get_objects(client: httpx.AsyncClient, obj_type: str) -> dict[str, dict[str, Any]]:
    """Fetch all XO objects of a given type. Returns {id: record}."""
    result = await _rpc(client, "xo.getAllObjects", {"filter": {"type": obj_type}})
    if isinstance(result, dict):
        return result
    return {}


def _host_node(xo_id: str, rec: dict[str, Any]) -> dict[str, Any] | None:
    uuid = rec.get("uuid") or xo_id
    name = rec.get("name_label") or uuid
    mem = rec.get("memory") or {}
    cpus = rec.get("cpus") or {}
    return {
        "id": f"xcpng-host-{uuid}",
        "label": name,
        "type": "xcpng",
        "ieee_address": f"xcpng-host-{uuid}",
        "hostname": name,
        "ip": rec.get("address") or None,
        "status": "online",
        "cpu_count": _int_or_none(cpus.get("cores")),
        "ram_gb": _gb(mem.get("size")),
        "disk_gb": None,
        "vendor": "XCP-ng",
        "model": rec.get("version") or "Hypervisor",
        "parent_ieee": None,
        "_xo_id": xo_id,
    }


def _vm_node(
    xo_id: str,
    rec: dict[str, Any],
    host_ieee: str | None,
    mac: str | None,
) -> dict[str, Any] | None:
    uuid = rec.get("uuid") or xo_id
    name = rec.get("name_label") or f"vm-{uuid}"
    power = rec.get("power_state", "Halted")
    mem = rec.get("memory") or {}
    cpus = rec.get("CPUs") or {}
    return {
        "id": f"xcpng-vm-{uuid}",
        "label": name,
        "type": "vm",
        "ieee_address": f"xcpng-vm-{uuid}",
        "hostname": name,
        "ip": None,
        "mac": mac,
        "status": "online" if power == "Running" else "offline",
        "cpu_count": _int_or_none(cpus.get("number") or cpus.get("max")),
        "ram_gb": _gb(mem.get("size")),
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

    # XO id → ieee map for hosts
    host_ieee: dict[str, str] = {}
    for xo_id, rec in hosts_raw.items():
        node = _host_node(xo_id, rec)
        if node and node["id"] not in seen:
            seen.add(node["id"])
            host_ieee[xo_id] = node["ieee_address"]
            # Strip internal key before appending
            nodes.append({k: v for k, v in node.items() if k != "_xo_id"})

    # XO VM id → first MAC from VIFs
    vm_mac: dict[str, str | None] = {}
    for _ref, vif in vifs_raw.items():
        vm_ref = vif.get("$VM") or vif.get("VM")
        if not vm_ref or vm_ref in vm_mac:
            continue
        raw_mac = vif.get("MAC") or ""
        vm_mac[vm_ref] = normalize_mac(raw_mac) if raw_mac else None

    # Filter and build VMs
    for xo_id, rec in vms_raw.items():
        if rec.get("is_template"):
            continue
        # XO uses type="VM" for real VMs, "VM-template" for templates
        if rec.get("type", "VM") != "VM":
            continue

        uuid = rec.get("uuid") or xo_id
        # $container is the host XO ID for running VMs; may be absent when halted
        container = rec.get("$container") or rec.get("resident_on") or ""
        parent_ieee = host_ieee.get(container) if container else None
        if parent_ieee is None and len(host_ieee) == 1:
            parent_ieee = next(iter(host_ieee.values()))

        mac = vm_mac.get(xo_id)
        node = _vm_node(xo_id, rec, parent_ieee, mac)
        if node and node["id"] not in seen:
            seen.add(node["id"])
            nodes.append(node)
            if parent_ieee:
                edges.append({"source": parent_ieee, "target": node["ieee_address"]})

    return nodes, edges


async def fetch_xcpng_inventory(
    host: str,
    username: str,
    password: str,
    verify_tls: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch hosts + VMs from XO. Returns (nodes, edges).

    Raises:
        ConnectionError: transport / auth / API failures (sanitized).
        ValueError: malformed response.
    """
    try:
        async with _make_client(host, verify_tls) as client:
            await _sign_in(client, username, password)
            hosts_raw = await _get_objects(client, "host")
            vms_raw = await _get_objects(client, "VM")
            vifs_raw = await _get_objects(client, "VIF")
        return _parse_inventory(hosts_raw, vms_raw, vifs_raw)
    except ConnectionError:
        raise
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            raise ConnectionError("Authentication failed — check XCPNG_USERNAME and XCPNG_PASSWORD") from exc
        raise ConnectionError(f"XO server returned HTTP {code}") from exc
    except httpx.HTTPError as exc:
        raise ConnectionError(_sanitize_error(exc)) from exc
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
        async with _make_client(host, verify_tls) as client:
            await _sign_in(client, username, password)
            hosts = await _get_objects(client, "host")
            vms_all = await _get_objects(client, "VM")
            n_vms = sum(
                1 for rec in vms_all.values()
                if not rec.get("is_template") and rec.get("type", "VM") == "VM"
            )
        return True, f"Connected to XO — {len(hosts)} host(s), {n_vms} VM(s)"
    except ConnectionError as exc:
        return False, str(exc)
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
    props.append({"key": "Source", "value": "XCP-ng / XO", "icon": None, "visible": False})
    return props
