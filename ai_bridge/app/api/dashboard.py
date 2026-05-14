"""Dashboard API — read-only JSON endpoints backed by the signal audit log.

Kept intentionally minimal so the dashboard stays a single HTML page with
vanilla JS fetch calls. No auth layer on these endpoints by default —
deploy behind Caddy basic_auth or a Cloudflare Access tunnel if you want
public access gated.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Body, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.risk.trade_plan import compute_outcome_pnl
from app.storage.signal_log import SignalLog

router = APIRouter(prefix="/api", tags=["dashboard"])

# Single shared instance — mirrors the pattern used in webhook.py so the
# underlying SQLite file is the same one the webhook writes to.
_signal_log = SignalLog()


@router.get("/gold-price")
async def get_live_gold_price():
    """Fetch live gold spot price (XAU/USD) via multi-provider fallback.

    This endpoint is independent from signal log — always fetches fresh data.
    Used by the dashboard to display current gold price without relying on
    stale signal log entries.

    Returns JSON: {price, symbol, unit, timestamp, status}
    """
    try:
        from app.context.market_context import get_gold_price

        price = await get_gold_price()
        return {
            "price": price,
            "symbol": "XAU/USD",
            "unit": "USD/troy oz",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "live",
        }
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error fetching gold price: {e}",
        ) from e


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


@router.get("/winrate")
async def winrate(
    hours: int = Query(
        24 * 30,
        ge=1,
        le=24 * 365,
        description="Lookback window (default: 30 days)",
    ),
) -> dict:
    """Win / loss breakdown per Pine-signal name.

    Only counts signals whose ``outcome`` has been filled in via
    ``PATCH /api/signals/{id}/outcome``. Until then, the response contains
    empty lists and the dashboard shows a hint.
    """
    return await _signal_log.winrate_by_signal(since_hours=hours)


class OutcomeUpdate(BaseModel):
    outcome: Literal["win", "loss", "breakeven"]
    pnl: float | None = Field(
        default=None,
        description=(
            "Account-currency PnL (positive=profit). Optional — if omitted "
            "and exit_price is provided, PnL is auto-computed from the "
            "stored trade plan (entry, stop_distance, lot)."
        ),
    )
    exit_price: float | None = Field(
        default=None,
        description=(
            "Price at which the trade was closed. When provided, both "
            "pnl_r and pnl (if lot known) are auto-computed. This is "
            "the preferred way for the dashboard to record outcomes."
        ),
    )


@router.patch("/signals/{signal_id}/outcome")
async def update_outcome(
    signal_id: int,
    body: OutcomeUpdate = Body(...),  # noqa: B008 — FastAPI DI idiom
) -> dict:
    """Record the real trade outcome for a signal (call after trade closes).

    If ``exit_price`` is provided, PnL is auto-derived from the stored
    trade plan:

      * ``pnl_r``  = (exit-entry) / stop_distance  (signed by side)
      * ``pnl``    = pnl_points × 100 × lot        (XAUUSD standard)

    If only ``outcome`` + ``pnl`` are sent, we store what we got.
    """
    existing = await _signal_log.get_by_id(signal_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Signal id={signal_id} not found",
        )

    pnl = body.pnl
    pnl_r: float | None = None
    exit_price = body.exit_price

    # Auto-compute from stored plan when exit_price is provided
    if exit_price is not None:
        plan = existing.get("plan") or {}
        side = plan.get("side") or existing.get("plan_side")
        entry = plan.get("entry_price") or existing.get("plan_entry")
        stop_distance = plan.get("stop_distance")
        lot = plan.get("lot_estimate") or existing.get("plan_lot")
        if side and entry is not None and stop_distance:
            try:
                pnl_r_auto, pnl_usd_auto = compute_outcome_pnl(
                    side=side,
                    entry_price=float(entry),
                    exit_price=float(exit_price),
                    stop_distance=float(stop_distance),
                    lot=float(lot) if lot else None,
                )
                pnl_r = pnl_r_auto
                if pnl is None and pnl_usd_auto is not None:
                    pnl = pnl_usd_auto
            except (TypeError, ValueError):
                # Fall through — the user-provided values (if any) will be stored
                pass

    try:
        ok = await _signal_log.update_outcome(
            signal_id,
            outcome=body.outcome,
            pnl=pnl,
            exit_price=exit_price,
            pnl_r=pnl_r,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Signal id={signal_id} not found",
        )
    return {
        "updated": True,
        "id": signal_id,
        "outcome": body.outcome,
        "exit_price": exit_price,
        "pnl": pnl,
        "pnl_r": pnl_r,
    }
