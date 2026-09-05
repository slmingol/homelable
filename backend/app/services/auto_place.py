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

INFRA_NODE_WIDTH = 320    # horizontal slot for infra devices
INFRA_TIER_HEIGHT = 280   # vertical gap between infra tiers
CLIENT_NODE_WIDTH = 220   # horizontal slot for client/non-infra devices
CLIENT_NODE_HEIGHT = 70   # vertical slot per client row
CLIENT_TIER_HEIGHT = 350  # vertical gap from last infra tier to client area
CLIENT_MAX_PER_ROW = 10   # clients per row in the flat client area
LLDP_TIMEOUT = 5.0

# Tier-0 roots (highest in topology hierarchy)
_ROOT_TYPES = {"router", "gateway", "firewall"}

# Only walk SNMP/LLDP on infrastructure devices — end devices don't run SNMP
_INFRA_TYPES = {"router", "gateway", "firewall", "switch", "ap"}


def _dev_in_types(dev: "InventoryDevice", types: set[str]) -> bool:
    """True if the device's type OR suggested_type is in types.

    Checks both fields so that devices where a generic IP scan set type='server'
    but the UniFi sync set suggested_type='switch' are still recognized.
    """
    return (
        (dev.type or "").lower() in types
        or (dev.suggested_type or "").lower() in types
    )


async def _build_topology(
    devices: list[InventoryDevice],
) -> tuple[dict[str, set[str]], dict[str, int], set[str]]:
    """Return (adjacency, stp_by_dev, confirmed_infra_ids).

    confirmed_infra_ids is the set of device IDs confirmed as real UniFi-managed
    infrastructure (appeared in /stat/device). Empty when UniFi is unconfigured.

    Sources (all non-fatal — partial results are usable):
      A. UniFi LLDP edges between infrastructure devices
      B. UniFi client-uplink associations (client -> AP/switch)
      C. SNMP/LLDP walks on infrastructure-typed approved devices only
    """
    # Build MAC / name lookup tables
    mac_to_dev: dict[str, str] = {}
    name_to_dev: dict[str, str] = {}
    dev_label: dict[str, str] = {}
    for dev in devices:
        if dev.mac:
            mac_to_dev[dev.mac.lower()] = dev.id
        if dev.hostname:
            name_to_dev[dev.hostname.lower()] = dev.id
        if dev.label:
            name_to_dev[dev.label.lower()] = dev.id
        dev_label[dev.id] = dev.label or dev.hostname or dev.ip or dev.id

    adjacency: dict[str, set[str]] = {}
    # device_id -> STP bridge priority (lower = root bridge candidate)
    stp_by_dev: dict[str, int] = {}
    # device IDs confirmed as real UniFi-managed infra (/stat/device)
    confirmed_infra_ids: set[str] = {}

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
            resolved_lldp: list[str] = []
            unresolved_lldp: list[str] = []
            for mac_a, mac_b in topo.get("lldp_edges", []):
                dev_a = mac_to_dev.get(mac_a)
                dev_b = mac_to_dev.get(mac_b)
                if dev_a and dev_b:
                    _add_edge(dev_a, dev_b)
                    resolved_lldp.append(f"{dev_label.get(dev_a, mac_a)} ↔ {dev_label.get(dev_b, mac_b)}")
                else:
                    unresolved_lldp.append(
                        f"{mac_a}({'ok' if dev_a else 'NO'}) ↔ {mac_b}({'ok' if dev_b else 'NO'})"
                    )
            if resolved_lldp:
                logger.info("auto_place: resolved LLDP pairs:\n  %s", "\n  ".join(resolved_lldp))
            if unresolved_lldp:
                logger.info("auto_place: unresolved LLDP MACs (not in DB):\n  %s", "\n  ".join(unresolved_lldp))
                logger.info("auto_place: DB MACs for lookup: %s", ", ".join(sorted(mac_to_dev.keys())))

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

            # Device uplinks: each UniFi device reports its upstream device MAC.
            # Devices whose uplink MAC doesn't resolve (e.g. the router is not
            # a UniFi device) are "core" switches — wire them to every BFS root
            # so BFS can traverse the full switch hierarchy.
            root_macs: set[str] = {
                dev.mac.lower() for dev in devices
                if dev.mac and _dev_in_types(dev, _ROOT_TYPES)
            }
            core_switches: list[str] = []
            for dev_mac, uplink_mac in topo.get("device_uplinks", {}).items():
                dev_id = mac_to_dev.get(dev_mac)
                uplink_id = mac_to_dev.get(uplink_mac)
                if dev_id and uplink_id:
                    _add_edge(dev_id, uplink_id)
                elif dev_id and not uplink_id:
                    # Uplink not in DB — this device's upstream is a non-managed
                    # device (e.g. pfsense/opnsense router). Treat it as a core switch.
                    core_switches.append(dev_id)

            if core_switches:
                # Connect core switches to all BFS roots so BFS can traverse down
                root_ids = [
                    mac_to_dev[m] for m in root_macs if m in mac_to_dev
                ]
                for root_id in root_ids:
                    for core_id in core_switches:
                        _add_edge(root_id, core_id)
                logger.info(
                    "auto_place: core switch(es) wired to BFS roots: %s → %s",
                    [dev_label.get(c, c) for c in core_switches],
                    [dev_label.get(r, r) for r in root_ids],
                )

            # Confirmed infra: every device that appeared in /stat/device
            for infra_mac in topo.get("infra_macs", {}):
                dev_id = mac_to_dev.get(infra_mac)
                if dev_id:
                    confirmed_infra_ids.add(dev_id)

            # STP priorities: translate MAC keys to device IDs
            for stp_mac, stp_prio in topo.get("stp_priorities", {}).items():
                dev_id = mac_to_dev.get(stp_mac)
                if dev_id:
                    stp_by_dev[dev_id] = stp_prio

        except Exception as exc:
            logger.warning("auto_place: UniFi topology fetch failed: %s", exc)

    # --- B. SNMP/LLDP — infrastructure devices only -------------------------
    snmp_infra = [
        d for d in devices
        if d.snmp_enabled and d.ip
        and _dev_in_types(d, _INFRA_TYPES)
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

    return adjacency, stp_by_dev, confirmed_infra_ids


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
    adjacency, stp_by_dev, confirmed_infra_ids = await _build_topology(all_devices)

    devices = approved_devices
    _dev_label = {dev.id: (dev.label or dev.hostname or dev.ip or dev.id) for dev in devices}

    # --- 4. BFS tier layout from root devices ------------------------------
    roots: list[str] = [
        d.id for d in devices
        if _dev_in_types(d, _ROOT_TYPES)
    ]

    if not roots:
        if adjacency:
            approved_ids: set[str] = {d.id for d in devices}
            # 1. Lowest STP bridge priority among approved switches = root bridge.
            #    Priority 0 < 4096 < 8192 … — the switch explicitly elected as root.
            stp_candidates = {
                dev_id: prio for dev_id, prio in stp_by_dev.items()
                if dev_id in approved_ids
            }
            if stp_candidates:
                roots = [min(stp_candidates, key=stp_candidates.__getitem__)]
                logger.info(
                    "auto_place: BFS root selected by lowest STP priority (%d): %s",
                    stp_candidates[roots[0]],
                    _dev_label.get(roots[0], roots[0]),
                )
            else:
                # 2. Approved switch directly connected to a root-typed device
                #    (e.g. the switch whose port faces pfsense/opnsense).
                root_typed_ids: set[str] = {
                    d.id for d in all_devices if _dev_in_types(d, _ROOT_TYPES)
                }
                near_root = [
                    dev_id for dev_id in approved_ids
                    if any(n in root_typed_ids for n in adjacency.get(dev_id, set()))
                ]
                if near_root:
                    roots = [max(near_root, key=lambda k: len(adjacency.get(k, set())))]
                    logger.info(
                        "auto_place: BFS root selected by proximity to firewall/router: %s",
                        _dev_label.get(roots[0], roots[0]),
                    )
                else:
                    # 3. Last resort: most-connected node.
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

    # --- 5. Compute positions (two-phase) ------------------------------------
    # Phase 1: place infra devices in a clean BFS tree using infra-only edges.
    # Phase 2: place all other (client) devices below, sorted by their infra
    # parent so clients of the same switch/AP appear adjacent.

    approved_set: set[str] = {d.id for d in devices}
    # AP-typed devices are only treated as infra if they appear in the UniFi
    # /stat/device response (confirmed_infra_ids) or have SNMP enabled.
    # IP/ARP scanners mistype WiFi clients (Google Nest, Chromecast, etc.) as 'ap';
    # real access points always appear in the UniFi device inventory.
    # If UniFi is unconfigured (confirmed_infra_ids empty), trust type='ap' as-is.
    infra_ids: set[str] = {
        d.id for d in devices
        if _dev_in_types(d, _INFRA_TYPES)
        and (
            not _dev_in_types(d, {"ap"})
            or not confirmed_infra_ids          # no UniFi data → trust type field
            or d.id in confirmed_infra_ids
            or d.snmp_enabled
        )
    }

    # Infra-only adjacency — both endpoints must be infra devices
    infra_adj: dict[str, set[str]] = {
        k: {v for v in vs if v in infra_ids}
        for k, vs in adjacency.items()
        if k in infra_ids
    }

    # BFS from infra roots through infra-only edges
    infra_root_ids = [r for r in roots if r in infra_ids]
    if not infra_root_ids and infra_ids:
        # roots may be firewall/router type not in approved_set — find adjacent infra
        for r in roots:
            for nbr in adjacency.get(r, set()):
                if nbr in infra_ids:
                    infra_root_ids = [nbr]
                    break
            if infra_root_ids:
                break
        if not infra_root_ids:
            infra_root_ids = [next(iter(infra_ids))]

    infra_tier_map: dict[str, int] = {r: 0 for r in infra_root_ids}
    infra_bfs_parent: dict[str, str] = {}
    infra_queue = list(infra_root_ids)
    while infra_queue:
        cur = infra_queue.pop(0)
        for nbr in infra_adj.get(cur, set()):
            if nbr not in infra_tier_map:
                infra_tier_map[nbr] = infra_tier_map[cur] + 1
                infra_bfs_parent[nbr] = cur
                infra_queue.append(nbr)

    max_infra_tier = max(infra_tier_map.values(), default=0)
    for dev_id in infra_ids:
        if dev_id in approved_set and dev_id not in infra_tier_map:
            infra_tier_map[dev_id] = max_infra_tier + 1

    # Build tier groups (approved infra devices only), ordered by DFS traversal
    # of the infra tree so edges between tiers don't cross.
    def _dfs_infra(root_ids: list[str]) -> list[str]:
        order: list[str] = []
        visited: set[str] = set()
        stack = list(reversed(root_ids))
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            order.append(node)
            children = sorted(
                [n for n in infra_adj.get(node, set())
                 if infra_tier_map.get(n, -1) == infra_tier_map.get(node, -2) + 1],
                key=lambda x: x,
            )
            stack.extend(reversed(children))
        return order

    dfs_ordered = _dfs_infra(infra_root_ids)
    dfs_index = {dev_id: i for i, dev_id in enumerate(dfs_ordered)}

    infra_tiers_grouped: dict[int, list[str]] = {}
    for dev_id, t in infra_tier_map.items():
        if dev_id in approved_set:
            infra_tiers_grouped.setdefault(t, []).append(dev_id)
    for t_num in infra_tiers_grouped:
        infra_tiers_grouped[t_num].sort(key=lambda d: dfs_index.get(d, 999))

    if infra_tiers_grouped:
        logger.info(
            "auto_place: infra layout tiers (%d devices):\n  %s",
            sum(len(v) for v in infra_tiers_grouped.values()),
            "\n  ".join(
                f"{_dev_label.get(d, d)}=t{t}"
                for t, ids in sorted(infra_tiers_grouped.items())
                for d in ids
            ),
        )

    position: dict[str, tuple[float, float]] = {}
    y_offset = 0.0
    for t_num, ids in sorted(infra_tiers_grouped.items()):
        row_width = len(ids) * INFRA_NODE_WIDTH
        start_x = -row_width / 2
        for col_idx, dev_id in enumerate(ids):
            position[dev_id] = (start_x + col_idx * INFRA_NODE_WIDTH, y_offset)
        y_offset += INFRA_TIER_HEIGHT

    infra_y_bottom = y_offset  # top of client area

    # Phase 2: client (non-infra) devices — sorted by parent infra device so
    # clients of the same switch/AP appear together in the client area.
    client_parent_infra: dict[str, str | None] = {}
    for dev in devices:
        if dev.id in infra_ids:
            continue
        parent = None
        for nbr_id in adjacency.get(dev.id, set()):
            if nbr_id in infra_ids and nbr_id in position:
                parent = nbr_id
                break
        client_parent_infra[dev.id] = parent

    # Group then sort: parents ordered by their x position (left → right)
    parent_clients: dict[str, list[str]] = {}
    orphan_clients: list[str] = []
    for dev in devices:
        if dev.id in infra_ids:
            continue
        p = client_parent_infra.get(dev.id)
        if p:
            parent_clients.setdefault(p, []).append(dev.id)
        else:
            orphan_clients.append(dev.id)

    sorted_parents = sorted(
        parent_clients.keys(),
        key=lambda pid: position.get(pid, (0.0, 0.0))[0],
    )
    all_clients_ordered: list[str] = []
    for pid in sorted_parents:
        all_clients_ordered.extend(parent_clients[pid])
    all_clients_ordered.extend(orphan_clients)

    client_y_start = infra_y_bottom + CLIENT_TIER_HEIGHT
    if all_clients_ordered:
        total_width = min(len(all_clients_ordered), CLIENT_MAX_PER_ROW) * CLIENT_NODE_WIDTH
        cl_start_x = -total_width / 2
        for i, client_id in enumerate(all_clients_ordered):
            col = i % CLIENT_MAX_PER_ROW
            row = i // CLIENT_MAX_PER_ROW
            position[client_id] = (
                cl_start_x + col * CLIENT_NODE_WIDTH,
                client_y_start + row * CLIENT_NODE_HEIGHT,
            )

    fallback_y = client_y_start

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
        x, y = position.get(dev.id, (0.0, fallback_y))
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

    # On force re-layout, wipe all existing edges and redraw only infra edges.
    # This removes the old client→AP spider-web lines left from prior runs.
    if force and existing_edges:
        from sqlalchemy import delete as sa_delete
        await db.execute(sa_delete(Edge).where(Edge.design_id == design_id))
        existing_edges = []

    existing_edge_pairs: set[frozenset[str]] = {
        frozenset([e.source, e.target]) for e in existing_edges
    }

    edges_created = 0
    seen_pairs: set[frozenset[str]] = set()

    # Only draw edges between infrastructure devices (switch↔switch, switch↔AP,
    # router↔switch). Omitting client→AP/switch edges keeps the canvas readable;
    # tier position already shows which tier a client belongs to.
    dev_by_id: dict[str, InventoryDevice] = {d.id: d for d in devices}
    infra_dev_ids: set[str] = {
        d.id for d in devices if _dev_in_types(d, _INFRA_TYPES)
    }

    for dev_id, neighbors in adjacency.items():
        if dev_id not in infra_dev_ids:
            continue
        src_node = all_node_by_dev.get(dev_id)
        if not src_node:
            continue
        for neighbor_id in neighbors:
            if neighbor_id not in infra_dev_ids:
                continue
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
