"""One-shot topology auto-placement service.

Walk LLDP on every SNMP-enabled approved device, build a hierarchy from the
neighbor graph, then place unplaced devices on the target design using a simple
BFS tier layout.  Existing nodes are never moved.
"""
import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Edge, InventoryDevice, Node
from app.services.lldp import discover_neighbors

logger = logging.getLogger(__name__)

TIER_HEIGHT = 250
NODE_WIDTH = 200
LLDP_TIMEOUT = 5.0

# Device types treated as tier-0 roots when no LLDP parent is found.
_ROOT_TYPES = {"router", "gateway", "firewall", "switch"}


async def run_auto_place(
    design_id: str,
    db: AsyncSession,
    force: bool = False,
) -> dict[str, Any]:
    """Place (or re-layout) approved devices onto design_id using LLDP topology.

    force=False (default): only place devices not yet on the canvas.
    force=True: reposition ALL existing nodes using LLDP tier layout too.

    Returns a summary dict:
      nodes_placed    — new Node rows created
      nodes_moved     — existing nodes repositioned (force mode only)
      edges_created   — new Edge rows created
      skipped         — devices already on canvas (non-force mode)
    """
    # --- 1. Load approved devices -----------------------------------------
    devices: list[InventoryDevice] = (
        await db.execute(
            select(InventoryDevice).where(InventoryDevice.status == "approved")
        )
    ).scalars().all()

    if not devices:
        return {"nodes_placed": 0, "edges_created": 0, "skipped": 0}

    # Map device_id → device for quick lookup
    dev_by_id: dict[str, InventoryDevice] = {d.id: d for d in devices}

    # --- 2. Find which devices already have a node on this design ----------
    existing_nodes: list[Node] = (
        await db.execute(select(Node).where(Node.design_id == design_id))
    ).scalars().all()

    placed_device_ids: set[str] = {
        n.device_id for n in existing_nodes if n.device_id
    }

    # --- 3. Walk LLDP on SNMP-enabled devices ------------------------------
    snmp_devices = [d for d in devices if d.snmp_enabled and d.ip]

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

    results = await asyncio.gather(*[_walk(d) for d in snmp_devices], return_exceptions=True)

    # Build adjacency: chassis_id / sys_name → device_id
    # Also collect: for each device, which neighbor chassis IDs it sees
    mac_to_dev: dict[str, str] = {}
    name_to_dev: dict[str, str] = {}
    for dev in devices:
        if dev.mac:
            mac_to_dev[dev.mac.lower()] = dev.id
        if dev.hostname:
            name_to_dev[dev.hostname.lower()] = dev.id
        label = (dev.label or "").lower()
        if label:
            name_to_dev[label] = dev.id

    # device_id → set of neighbor device_ids (from LLDP)
    lldp_neighbors: dict[str, set[str]] = {}

    for result in results:
        if isinstance(result, Exception):
            continue
        dev_id, neighbors = result
        for n in neighbors:
            chassis = (n.get("chassis_id") or "").lower().strip()
            sys_name = (n.get("sys_name") or "").lower().strip()
            neighbor_dev_id = mac_to_dev.get(chassis) or name_to_dev.get(sys_name)
            if neighbor_dev_id and neighbor_dev_id != dev_id:
                lldp_neighbors.setdefault(dev_id, set()).add(neighbor_dev_id)
                lldp_neighbors.setdefault(neighbor_dev_id, set()).add(dev_id)

    # --- 4. Determine tier / BFS from roots --------------------------------
    roots: list[str] = []
    for dev in devices:
        dev_type = (dev.type or dev.suggested_type or "").lower()
        if dev_type in _ROOT_TYPES:
            roots.append(dev.id)

    if not roots:
        # Fall back: devices with the most LLDP neighbors
        if lldp_neighbors:
            roots = [max(lldp_neighbors, key=lambda k: len(lldp_neighbors[k]))]
        else:
            roots = [devices[0].id]

    tier: dict[str, int] = {}
    queue = list(roots)
    for r in roots:
        tier[r] = 0

    while queue:
        current_id = queue.pop(0)
        for neighbor_id in lldp_neighbors.get(current_id, set()):
            if neighbor_id not in tier:
                tier[neighbor_id] = tier[current_id] + 1
                queue.append(neighbor_id)

    # Devices with no LLDP at all get a catch-all tier at the bottom
    max_tier = max(tier.values(), default=0)
    ungrouped: list[str] = [d.id for d in devices if d.id not in tier]
    for dev_id in ungrouped:
        tier[dev_id] = max_tier + 1

    # --- 5. Compute layout positions ---------------------------------------
    tiers: dict[int, list[str]] = {}
    for dev_id, t in tier.items():
        tiers.setdefault(t, []).append(dev_id)

    position: dict[str, tuple[float, float]] = {}
    for t_num, ids in sorted(tiers.items()):
        y = t_num * TIER_HEIGHT
        total_width = len(ids) * NODE_WIDTH
        start_x = -total_width / 2
        for i, dev_id in enumerate(ids):
            x = start_x + i * NODE_WIDTH
            position[dev_id] = (x, y)

    # --- 6. Create Node rows for unplaced devices (or reposition all) -------
    nodes_placed = 0
    nodes_moved = 0
    skipped = 0
    new_node_by_dev: dict[str, str] = {}

    # Build existing device→node map for edge creation
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
        node = Node(
            id=node_id,
            type=node_type,
            label=label,
            design_id=design_id,
            device_id=dev.id,
            pos_x=x,
            pos_y=y,
        )
        db.add(node)
        new_node_by_dev[dev.id] = node_id
        nodes_placed += 1

    await db.flush()

    all_node_by_dev = {**existing_node_by_dev, **new_node_by_dev}

    # --- 7. Create Edge rows for LLDP pairs --------------------------------
    # Existing edges on this design (source,target) to avoid dupes
    existing_edges: list[Edge] = (
        await db.execute(select(Edge).where(Edge.design_id == design_id))
    ).scalars().all()
    existing_edge_pairs: set[frozenset[str]] = {
        frozenset([e.source, e.target]) for e in existing_edges
    }

    edges_created = 0
    seen_pairs: set[frozenset[str]] = set()

    for dev_id, neighbors in lldp_neighbors.items():
        src_node = all_node_by_dev.get(dev_id)
        if not src_node:
            continue
        for neighbor_id in neighbors:
            pair = frozenset([src_node, all_node_by_dev.get(neighbor_id, "")])
            if "" in pair:
                continue
            if pair in existing_edge_pairs or pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            tgt_node = all_node_by_dev[neighbor_id]
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
