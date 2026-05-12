"""Dashboard API — read-only JSON endpoints backed by the signal audit log.

Kept intentionally minimal so the dashboard stays a single HTML page with
vanilla JS fetch calls. No auth layer on these endpoints by default —
deploy behind Caddy basic_auth or a Cloudflare Access tunnel if you want
public access gated.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.storage.signal_log import SignalLog


router = APIRouter(prefix="/api", tags=["dashboard"])

# Single shared instance — mirrors the pattern used in webhook.py so the
# underlying SQLite file is the same one the webhook writes to.
_signal_log = SignalLog()


@router.get("/stats")
async def stats(
    hours: int = Query(24, ge=1, le=24 * 30, description="Lookback window"),
) -> dict:
    """Return rolling counters for the dashboard header."""
    return await _signal_log.stats(since_hours=hours)


@router.get("/signals")
async def list_signals(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    action: str | None = Query(
        None,
        description="Filter by decision action (execute|skip|reduce)",
    ),
    symbol: str | None = Query(None, description="Filter by exact symbol"),
) -> dict:
    """Newest-first list of signals. Supports filter + pagination."""
    if action and action not in {"execute", "skip", "reduce"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="action must be one of: execute, skip, reduce",
        )
    items = await _signal_log.list_recent(
        limit=limit, offset=offset, action=action, symbol=symbol
    )
    return {"items": items, "limit": limit, "offset": offset, "count": len(items)}


@router.get("/signals/{signal_id}")
async def get_signal(signal_id: int) -> dict:
    """Full detail of one signal including parsed alert/context/decision JSON."""
    data = await _signal_log.get_by_id(signal_id)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Signal id={signal_id} not found",
        )
    return data
