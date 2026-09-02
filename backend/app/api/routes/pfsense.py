"""FastAPI router for pfSense import + auto-sync config."""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.scheduler import reschedule_pfsense_sync, set_pfsense_sync_enabled
from app.db.database import AsyncSessionLocal, get_db
from app.db.models import InventoryDevice, ScanRun
from app.schemas.pfsense import (
    PfsenseConfig,
    PfsenseImportResponse,
    PfsenseSyncConfig,
    PfsenseTestConnectionResponse,
)
from app.services.discovery_sources import add_source
from app.services.pfsense_service import fetch_pfsense_inventory, test_pfsense_connection

logger = logging.getLogger(__name__)
router = APIRouter()

_SOURCE = "pfsense"


@router.post("/test-connection", response_model=PfsenseTestConnectionResponse)
async def test_connection_endpoint(
    _: str = Depends(get_current_user),
) -> PfsenseTestConnectionResponse:
    if not (settings.pfsense_host and settings.pfsense_api_key):
        raise HTTPException(
            status_code=400,
            detail="No pfSense host/credentials configured. Set PFSENSE_HOST and PFSENSE_API_KEY in the server .env.",
        )
    connected, message = await test_pfsense_connection(
        host=settings.pfsense_host,
        port=settings.pfsense_port,
        api_key=settings.pfsense_api_key,
        scheme=settings.pfsense_scheme,
        verify_tls=settings.pfsense_verify_tls,
    )
    return PfsenseTestConnectionResponse(connected=connected, message=message)


@router.post("/sync-now", response_model=PfsenseImportResponse)
async def sync_pfsense_now(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> PfsenseImportResponse:
    if not (settings.pfsense_host and settings.pfsense_api_key):
        raise HTTPException(
            status_code=400,
            detail="Cannot sync: no pfSense host/credentials configured in the server env.",
        )
    try:
        devices = await fetch_pfsense_inventory(
            host=settings.pfsense_host,
            port=settings.pfsense_port,
            api_key=settings.pfsense_api_key,
            scheme=settings.pfsense_scheme,
            verify_tls=settings.pfsense_verify_tls,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return await _persist_devices(db, devices)


@router.get("/config", response_model=PfsenseConfig)
async def get_pfsense_config(_: str = Depends(get_current_user)) -> PfsenseConfig:
    return PfsenseConfig(
        host=settings.pfsense_host,
        port=settings.pfsense_port,
        verify_tls=settings.pfsense_verify_tls,
        sync_enabled=settings.pfsense_sync_enabled,
        sync_interval=settings.pfsense_sync_interval,
        credentials_configured=bool(settings.pfsense_host and settings.pfsense_api_key),
    )


@router.post("/config", response_model=PfsenseConfig)
async def save_pfsense_config(
    payload: PfsenseSyncConfig,
    _: str = Depends(get_current_user),
) -> PfsenseConfig:
    if payload.sync_enabled and not (settings.pfsense_host and settings.pfsense_api_key):
        raise HTTPException(
            status_code=400,
            detail="Cannot enable auto-sync: no pfSense host/credentials configured in the server env.",
        )
    try:
        settings.pfsense_sync_enabled = payload.sync_enabled
        settings.pfsense_sync_interval = payload.sync_interval
        settings.save_overrides()
        set_pfsense_sync_enabled(payload.sync_enabled)
        if payload.sync_enabled:
            reschedule_pfsense_sync(payload.sync_interval)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return await get_pfsense_config()


async def _persist_devices(
    db: AsyncSession,
    devices: list[dict[str, Any]],
) -> PfsenseImportResponse:
    pending_created = 0
    pending_updated = 0

    for dev in devices:
        ieee = dev.get("ieee_address")
        if not ieee:
            continue
        ip = dev.get("ip")
        mac = dev.get("mac")

        filters = [InventoryDevice.ieee_address == ieee]
        if ip:
            filters.append(InventoryDevice.ip == ip)
        if mac:
            filters.append(InventoryDevice.mac == mac)

        existing = (
            await db.execute(select(InventoryDevice).where(or_(*filters)))
        ).scalars().first()

        if existing is None:
            row = InventoryDevice(
                ieee_address=ieee,
                ip=ip,
                mac=mac,
                hostname=dev.get("hostname"),
                friendly_name=dev.get("label"),
                suggested_type=dev.get("type"),
                vendor=dev.get("vendor"),
                model=dev.get("model"),
                properties=dev.get("properties", []),
                status="pending",
                discovery_source=_SOURCE,
                discovery_sources=[_SOURCE],
            )
            db.add(row)
            pending_created += 1
        else:
            existing.discovery_sources = add_source(
                list(existing.discovery_sources or []), _SOURCE
            )
            existing.ip = ip or existing.ip
            existing.mac = existing.mac or mac
            existing.hostname = dev.get("hostname") or existing.hostname
            existing.friendly_name = dev.get("label") or existing.friendly_name
            if existing.status == "approved":
                existing.status = "pending"
            pending_updated += 1

    await db.commit()
    return PfsenseImportResponse(
        device_count=len(devices),
        pending_created=pending_created,
        pending_updated=pending_updated,
    )


async def _background_pfsense_sync(run_id: str) -> None:
    async with AsyncSessionLocal() as db:
        try:
            devices = await fetch_pfsense_inventory(
                host=settings.pfsense_host,
                port=settings.pfsense_port,
                api_key=settings.pfsense_api_key,
                verify_tls=settings.pfsense_verify_tls,
            )
            result = await _persist_devices(db, devices)
            run = await db.get(ScanRun, run_id)
            if run:
                run.status = "done"
                run.devices_found = result.device_count
                run.finished_at = datetime.now(timezone.utc)
                await db.commit()
            from app.api.routes.status import broadcast_scan_update
            await broadcast_scan_update(run_id=run_id, devices_found=result.device_count)
        except Exception as exc:
            logger.exception("pfSense sync %s failed", run_id)
            await db.rollback()
            run = await db.get(ScanRun, run_id)
            if run:
                run.status = "error"
                run.error = str(exc)[:500]
                run.finished_at = datetime.now(timezone.utc)
                await db.commit()
