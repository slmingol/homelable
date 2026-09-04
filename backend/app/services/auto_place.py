"""One-shot topology auto-placement service.

Build a topology graph from UniFi LLDP/client-association data and
SNMP/LLDP walks on infrastructure-typed devices only, then place (or
re-layout) approved devices on the target design using a BFS tier layout.
"""
import asyncio
import logging
import uuid
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as app_settings
from app.db.models import Edge, InventoryDevice, Node
from app.services.lldp import discover_neighbors
from app.services.unifi_service import fetch_unifi_topology

logger = logging.getLogger(__name__)

TIER_HEIGHT = 250
NODE_WIDTH = 200
LLDP_TIMEOUT = 5.0

# Tier-0 roots (highest in topology hierarchy)
_ROOT_TYPES = {"router", "gateway", "firewall"}

# Only walk SNMP/LLDP on infrastructure devices — end devices don't run SNMP
_INFRA_TYPES = {"router", "gateway", "firewall", "switch", "ap"}


async def _build_topology(
    devices: list[InventoryDevice],
) -> dict[str, set[str]]:
    """Return adjacency map: device_id -> set of neighbor device_ids.

    Sources (all non-fatal — partial results are usable):
      A. UniFi LLDP edges between infrastructure devices
      B. UniFi client-uplink associations (client -> AP/switch)
      C. SNMP/LLDP walks on infrastructure-typed approved devices only
    """
    # Build MAC / name lookup tables
    mac_to_dev: dict[str, str] = {}
    name_to_dev: dict[str, str] = {}
    for dev in devices:
        if dev.mac:
            mac_to_dev[dev.mac.lower()] = dev.id
        if dev.hostname:
            name_to_dev[dev.hostname.lower()] = dev.id
        if dev.label:
            name_to_dev[dev.label.lower()] = dev.id

    adjacency: dict[str, set[str]] = {}

    def _add_edge(dev_a: str, dev_b: str) -> None:
        if dev_a != dev_b:
            adjacency.setdefault(dev_a, set()).add(dev_b)
            adjacency.setdefault(dev_b, set()).add(dev_a)

    # --- A. UniFi topology ---------------------------------------------------
    s = app_settings
    host = s.unifi_effective_host
    port = s.unifi_effective_port
    if host and s.unifi_username and s.unifi_password:
        try:
            topo = await fetch_unifi_topology(
                host=host,
                port=port,
                site=s.unifi_site or "default",
                username=s.unifi_username,
                password=s.unifi_password,
                verify_tls=s.unifi_verify_tls,
            )
            # Infrastructure LLDP links
            for mac_a, mac_b in topo.get("lldp_edges", []):
                dev_a = mac_to_dev.get(mac_a)
                dev_b = mac_to_dev.get(mac_b)
                if dev_a and dev_b:
                    _add_edge(dev_a, dev_b)

            # Client -> AP/switch uplinks
            unmatched_clients: list[str] = []
            unmatched_uplinks: list[str] = []
            matched_uplinks = 0
            for client_mac, uplink_mac in topo.get("client_uplinks", {}).items():
                dev_client = mac_to_dev.get(client_mac)
                dev_uplink = mac_to_dev.get(uplink_mac)
                if dev_client and dev_uplink:
                    _add_edge(dev_client, dev_uplink)
                    matched_uplinks += 1
                else:
                    if not dev_client:
                        unmatched_clients.append(client_mac)
                    if not dev_uplink:
                        unmatched_uplinks.append(uplink_mac)

            logger.info(
                "auto_place: UniFi topology — %d LLDP edges, %d client uplinks "
                "(%d resolved, %d unmatched clients, %d unmatched uplinks)",
                len(topo.get("lldp_edges", [])),
                len(topo.get("client_uplinks", {})),
                matched_uplinks,
                len(unmatched_clients),
                len(set(unmatched_uplinks)),
            )
            if unmatched_clients:
                logger.info(
                    "auto_place: unmatched client MACs (not in DB): %s",
                    ", ".join(sorted(set(unmatched_clients))[:30]),
                )
            if unmatched_uplinks:
                logger.info(
                    "auto_place: unmatched uplink MACs (AP/switch not in DB): %s",
                    ", ".join(sorted(set(unmatched_uplinks))),
                )
        except Exception as exc:
            logger.warning("auto_place: UniFi topology fetch failed: %s", exc)

    # --- B. SNMP/LLDP — infrastructure devices only -------------------------
    snmp_infra = [
        d for d in devices
        if d.snmp_enabled and d.ip
        and (d.type or d.suggested_type or "").lower() in _INFRA_TYPES
    ]

    async def _walk(dev: InventoryDevice) -> tuple[str, list[dict[str, Any]]]:
        try:
            neighbors = await asyncio.wait_for(
                discover_neighbors(
                    host=dev.ip,
                    community=dev.snmp_community or "public",
                    port=dev.snmp_port or 161,
                ),
                timeout=LLDP_TIMEOUT,
            )
        except Exception:
            neighbors = []
        return dev.id, neighbors

    if snmp_infra:
        results = await asyncio.gather(*[_walk(d) for d in snmp_infra], return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                continue
            dev_id, neighbors = result
            for n in neighbors:
                chassis = (n.get("chassis_id") or "").lower().strip()
                sys_name = (n.get("sys_name") or "").lower().strip()
                neighbor_dev_id = mac_to_dev.get(chassis) or name_to_dev.get(sys_name)
                if neighbor_dev_id:
                    _add_edge(dev_id, neighbor_dev_id)
        logger.info("auto_place: SNMP walk on %d infra device(s)", len(snmp_infra))

    return adjacency


async def run_auto_place(
    design_id: str,
    db: AsyncSession,
    force: bool = False,
) -> dict[str, Any]:
    """Place (or re-layout) approved devices onto design_id using topology data.

    force=False (default): only place devices not yet on the canvas.
    force=True: reposition ALL existing nodes using the topology tier layout.

    Returns:
      nodes_placed    — new Node rows created
      nodes_moved     — existing nodes repositioned (force mode only)
      edges_created   — new Edge rows created
      skipped         — devices already on canvas (non-force mode)
    """
    # --- 1. Load devices -----------------------------------------------------
    # Approved devices are placed on canvas; all non-hidden devices are used
    # for MAC lookup during topology building so that pending infra devices
    # (switches, APs) still appear as edge endpoints.
    approved_devices: list[InventoryDevice] = (
        await db.execute(
            select(InventoryDevice).where(InventoryDevice.status == "approved")
        )
    ).scalars().all()

    if not approved_devices:
        return {"nodes_placed": 0, "nodes_moved": 0, "edges_created": 0, "skipped": 0}

    all_devices: list[InventoryDevice] = (
        await db.execute(
            select(InventoryDevice).where(InventoryDevice.status != "hidden")
        )
    ).scalars().all()

    # --- 2. Find which devices already have a node on this design ----------
    existing_nodes: list[Node] = (
        await db.execute(select(Node).where(Node.design_id == design_id))
    ).scalars().all()

    placed_device_ids: set[str] = {
        n.device_id for n in existing_nodes if n.device_id
    }

    # --- 3. Build topology adjacency from UniFi + SNMP ---------------------
    # Pass all non-hidden devices for MAC resolution; placement uses approved only.
    adjacency = await _build_topology(all_devices)

    devices = approved_devices

    # --- 4. BFS tier layout from root devices ------------------------------
    roots: list[str] = [
        d.id for d in devices
        if (d.type or d.suggested_type or "").lower() in _ROOT_TYPES
    ]

    if not roots:
        if adjacency:
            roots = [max(adjacency, key=lambda k: len(adjacency[k]))]
        else:
            roots = [devices[0].id]

    tier: dict[str, int] = {r: 0 for r in roots}
    queue = list(roots)
    while queue:
        current_id = queue.pop(0)
        for neighbor_id in adjacency.get(current_id, set()):
            if neighbor_id not in tier:
                tier[neighbor_id] = tier[current_id] + 1
                queue.append(neighbor_id)

    max_tier = max(tier.values(), default=0)
    for dev in devices:
        if dev.id not in tier:
            tier[dev.id] = max_tier + 1

    # --- 5. Compute positions ----------------------------------------------
    tiers: dict[int, list[str]] = {}
    for dev_id, t in tier.items():
        tiers.setdefault(t, []).append(dev_id)

    position: dict[str, tuple[float, float]] = {}
    for t_num, ids in sorted(tiers.items()):
        y = t_num * TIER_HEIGHT
        total_width = len(ids) * NODE_WIDTH
        start_x = -total_width / 2
        for i, dev_id in enumerate(ids):
            position[dev_id] = (start_x + i * NODE_WIDTH, y)

    # --- 6. Create / reposition Node rows ----------------------------------
    nodes_placed = 0
    nodes_moved = 0
    skipped = 0
    new_node_by_dev: dict[str, str] = {}

    existing_node_by_dev: dict[str, str] = {
        n.device_id: n.id for n in existing_nodes if n.device_id
    }
    existing_node_obj: dict[str, Node] = {
        n.device_id: n for n in existing_nodes if n.device_id
    }

    for dev in devices:
        x, y = position.get(dev.id, (0.0, float((max_tier + 2) * TIER_HEIGHT)))
        if dev.id in placed_device_ids:
            if force:
                node = existing_node_obj[dev.id]
                node.pos_x = x
                node.pos_y = y
                nodes_moved += 1
            else:
                skipped += 1
            continue

        node_id = str(uuid.uuid4())
        label = dev.label or dev.hostname or dev.mac or dev.ip or dev.id
        node_type = dev.type or dev.suggested_type or "device"
        db.add(Node(
            id=node_id,
            type=node_type,
            label=label,
            design_id=design_id,
            device_id=dev.id,
            pos_x=x,
            pos_y=y,
        ))
        new_node_by_dev[dev.id] = node_id
        nodes_placed += 1

    await db.flush()

    all_node_by_dev = {**existing_node_by_dev, **new_node_by_dev}

    # --- 7. Create Edge rows for topology pairs ----------------------------
    existing_edges: list[Edge] = (
        await db.execute(select(Edge).where(Edge.design_id == design_id))
    ).scalars().all()
    existing_edge_pairs: set[frozenset[str]] = {
        frozenset([e.source, e.target]) for e in existing_edges
    }

    edges_created = 0
    seen_pairs: set[frozenset[str]] = set()

    for dev_id, neighbors in adjacency.items():
        src_node = all_node_by_dev.get(dev_id)
        if not src_node:
            continue
        for neighbor_id in neighbors:
            tgt_node = all_node_by_dev.get(neighbor_id)
            if not tgt_node:
                continue
            pair = frozenset([src_node, tgt_node])
            if pair in existing_edge_pairs or pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            db.add(Edge(
                id=str(uuid.uuid4()),
                source=src_node,
                target=tgt_node,
                design_id=design_id,
                type="ethernet",
            ))
            edges_created += 1

    await db.commit()
    logger.info(
        "auto_place design=%s: placed=%d moved=%d edges=%d skipped=%d",
        design_id, nodes_placed, nodes_moved, edges_created, skipped,
    )
    return {
        "nodes_placed": nodes_placed,
        "nodes_moved": nodes_moved,
        "edges_created": edges_created,
        "skipped": skipped,
    }
