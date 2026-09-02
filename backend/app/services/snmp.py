"""Async SNMP polling via puresnmp."""
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_OIDS: list[dict[str, str]] = [
    {"oid": "1.3.6.1.2.1.1.1.0", "label": "sysDescr"},
    {"oid": "1.3.6.1.2.1.1.3.0", "label": "sysUpTime"},
    {"oid": "1.3.6.1.2.1.1.5.0", "label": "sysName"},
    {"oid": "1.3.6.1.2.1.2.1.0", "label": "ifNumber"},
]


async def poll_device(
    host: str,
    community: str = "public",
    version: str = "2c",
    port: int = 161,
    oids: list[dict[str, str]] | None = None,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Poll a device via SNMP GET. Returns list of {oid, label, value, value_type}."""
    try:
        from puresnmp.api.raw import Client, V2C  # type: ignore[import-untyped]
    except ImportError:
        logger.error("puresnmp not installed — SNMP polling unavailable")
        return []

    targets = oids if oids else DEFAULT_OIDS
    results: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()

    try:
        async with Client(host, V2C(community), port=port, timeout=timeout) as client:
            for target in targets:
                oid = target["oid"]
                label = target.get("label", oid)
                try:
                    raw = await client.get(oid)
                    value, vtype = _decode_value(raw)
                    results.append({"oid": oid, "label": label, "value": value, "value_type": vtype, "polled_at": now})
                except Exception as exc:
                    logger.debug("SNMP GET %s from %s failed: %s", oid, host, exc)
                    results.append({"oid": oid, "label": label, "value": None, "value_type": "error", "polled_at": now})
    except Exception as exc:
        logger.debug("SNMP connection to %s failed: %s", host, exc)

    return results


def _decode_value(raw: Any) -> tuple[str, str]:
    """Convert puresnmp raw value to (str_value, type_name)."""
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8", errors="replace").strip(), "string"
        except Exception:
            return raw.hex(), "bytes"
    if isinstance(raw, int):
        return str(raw), "integer"
    return str(raw), type(raw).__name__
