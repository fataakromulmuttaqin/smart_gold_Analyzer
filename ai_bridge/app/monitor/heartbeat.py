"""SmartGold Monitor — Telegram notifications for bot lifecycle events.

Sends notifications for:
  1. Bot startup / shutdown (running status)
  2. Signal SKIPPED by LLM or guards (with reason)
  3. Signal EXECUTED successfully (with details)
  4. No signal received due to errors (webhook errors, timeouts)
  5. Periodic heartbeat: warns if no signal received in MONITOR_SILENCE_HOURS

All sends are fire-and-forget — a failed notification never blocks the pipeline.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from app.config.settings import Settings, get_settings
from app.models.schemas import LLMDecision, MacroContext, TradingViewAlert
from app.utils.logging import logger


class Monitor:
    """Centralized monitoring notifier for the SmartGold AI Bridge."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._last_signal_ts: float = time.time()  # track last received signal
        self._heartbeat_task: asyncio.Task | None = None
        self._started_at: datetime = datetime.now(timezone.utc)

    # ══════════════════════════════════════════════════════════════════
    # Core Telegram sender
    # ══════════════════════════════════════════════════════════════════

    async def _send_telegram(self, text: str) -> bool:
        """Send a Markdown message to the configured Telegram chat."""
        if not self._settings.telegram_is_configured:
            logger.debug("Monitor: Telegram not configured — skipping")
            return False

        url = (
            f"https://api.telegram.org/bot{self._settings.telegram_bot_token}"
            f"/sendMessage"
        )
        payload = {
            "chat_id": self._settings.telegram_chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning(
                    "Monitor Telegram HTTP {}: {}",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
            return True
        except httpx.HTTPError as exc:
            logger.warning("Monitor Telegram send failed: {}", exc)
            return False

    # ══════════════════════════════════════════════════════════════════
    # 1. Bot Running — startup & shutdown
    # ══════════════════════════════════════════════════════════════════

    async def notify_startup(self) -> bool:
        """Send notification that the bot has started successfully."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        executor = "cTrader" if self._settings.ctrader_is_configured else (
            "MT5" if self._settings.mt5_is_configured else "Noop (signal-only)"
        )
        text = (
            f"🟢 *SmartGold AI Bridge ONLINE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ Started: `{now}`\n"
            f"🤖 Model: `{self._settings.minimax_model}`\n"
            f"🎯 Min Confidence: `{self._settings.min_confidence}`\n"
            f"📊 Executor: `{executor}`\n"
            f"🛡️ SL Policy: `{self._settings.sl_policy}`\n"
            f"⏱️ Cooldown: `{self._settings.signal_cooldown_seconds}s`\n"
            f"🔕 Mock LLM: `{self._settings.llm_mock_mode}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Monitoring active — will alert on skip/execute/error_"
        )
        return await self._send_telegram(text)

    async def notify_shutdown(self) -> bool:
        """Send notification that the bot is shutting down."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        uptime = datetime.now(timezone.utc) - self._started_at
        hours = int(uptime.total_seconds() // 3600)
        mins = int((uptime.total_seconds() % 3600) // 60)
        text = (
            f"🔴 *SmartGold AI Bridge OFFLINE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ Stopped: `{now}`\n"
            f"⏱️ Uptime: `{hours}h {mins}m`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Bot is shutting down. Signals will NOT be processed._"
        )
        return await self._send_telegram(text)

    # ══════════════════════════════════════════════════════════════════
    # 2. Signal SKIPPED (LLM decided skip, or guards blocked)
    # ══════════════════════════════════════════════════════════════════

    async def notify_signal_skipped(
        self,
        alert: TradingViewAlert,
        decision: LLMDecision,
        reason: str,
        blocked_by_guard: bool = False,
    ) -> bool:
        """Notify when a signal is skipped/blocked with the reason."""
        self._last_signal_ts = time.time()

        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        source = "🛡️ GUARD BLOCKED" if blocked_by_guard else "🧠 LLM SKIPPED"

        text = (
            f"⏸️ *Signal SKIPPED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 {alert.symbol} / {alert.timeframe}m — `{alert.signal}`\n"
            f"💰 Price: `{alert.price}`\n"
            f"⏰ Time: `{now}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚫 Source: *{source}*\n"
            f"📝 Reason: _{reason}_\n"
            f"📊 Confidence: `{decision.confidence:.2f}`\n"
        )

        if decision.risk_notes:
            text += f"⚠️ Risk: _{decision.risk_notes}_\n"

        # Add trend context
        trend = "BULL" if alert.bull_trend else ("BEAR" if alert.bear_trend else "NEUTRAL")
        text += (
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 Trend: `{trend}` | ATR: `${alert.atr or 0:.2f}`\n"
            f"📊 Vol Ratio: `{alert.volume_ratio or 0:.2f}x`"
        )

        return await self._send_telegram(text)

    # ══════════════════════════════════════════════════════════════════
    # 3. Signal EXECUTED (order placed or decision = execute/reduce)
    # ══════════════════════════════════════════════════════════════════

    async def notify_signal_executed(
        self,
        alert: TradingViewAlert,
        decision: LLMDecision,
        execution_result: dict | None = None,
        plan: dict | None = None,
    ) -> bool:
        """Notify when a signal is executed with full details."""
        self._last_signal_ts = time.time()

        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        action_emoji = "✅" if decision.action == "execute" else "⚠️"
        action_label = decision.action.upper()

        text = (
            f"{action_emoji} *Signal {action_label}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 {alert.symbol} / {alert.timeframe}m — `{alert.signal}`\n"
            f"💰 Price: `{alert.price}`\n"
            f"⏰ Time: `{now}`\n"
            f"📊 Confidence: `{decision.confidence:.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧠 *Reasoning:*\n_{decision.reasoning}_\n"
        )

        if decision.risk_notes:
            text += f"⚠️ *Risk:* _{decision.risk_notes}_\n"

        # Trade plan details
        if plan:
            plan_dict = plan.to_dict() if hasattr(plan, "to_dict") else (
                dict(plan) if not isinstance(plan, dict) else plan
            )
            sl = plan_dict.get("stop_loss")
            tp = plan_dict.get("take_profit")
            rr = plan_dict.get("risk_reward")
            lot = plan_dict.get("lot_estimate")
            side = plan_dict.get("side", "?")

            text += (
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 *Trade Plan:*\n"
                f"  Side: `{side.upper()}`\n"
            )
            if sl:
                text += f"  SL: `{sl:.2f}`\n"
            if tp:
                text += f"  TP: `{tp:.2f}`\n"
            if rr:
                text += f"  R:R: `1:{rr:.1f}`\n"
            if lot:
                text += f"  Lot: `{lot}`\n"

        # Execution result
        if execution_result:
            exec_dict = execution_result if isinstance(execution_result, dict) else (
                execution_result.to_dict() if hasattr(execution_result, "to_dict") else {}
            )
            if exec_dict.get("placed"):
                text += (
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏦 *Order Placed:*\n"
                    f"  Order ID: `{exec_dict.get('order_id', '?')}`\n"
                    f"  Volume: `{exec_dict.get('volume', '?')}`\n"
                    f"  Entry: `{exec_dict.get('entry_price', '?')}`\n"
                )
            else:
                text += (
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📝 _Order not placed (Noop executor / signal-only mode)_\n"
                )

        return await self._send_telegram(text)

    # ══════════════════════════════════════════════════════════════════
    # 4. No Signal — Error (webhook errors, pipeline failures)
    # ══════════════════════════════════════════════════════════════════

    async def notify_error(
        self,
        error_type: str,
        error_detail: str,
        alert: TradingViewAlert | None = None,
    ) -> bool:
        """Notify when an error prevents signal processing."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        text = (
            f"🚨 *ERROR — Signal NOT Processed*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ Time: `{now}`\n"
            f"❌ Type: `{error_type}`\n"
            f"📝 Detail: _{error_detail}_\n"
        )

        if alert:
            text += (
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📍 Signal: {alert.symbol} / {alert.timeframe}m — `{alert.signal}`\n"
                f"💰 Price: `{alert.price}`\n"
            )

        text += (
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Check server logs for details._\n"
            f"`docker logs smartgold-ai-bridge --tail 50`"
        )

        return await self._send_telegram(text)

    # ══════════════════════════════════════════════════════════════════
    # 5. Silence Alert — no signal received in X hours
    # ══════════════════════════════════════════════════════════════════

    async def notify_silence(self, hours_silent: float) -> bool:
        """Warn that no signal has been received for too long."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        text = (
            f"⚠️ *NO SIGNAL RECEIVED — {hours_silent:.1f}h*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ Check time: `{now}`\n"
            f"🕐 Last signal: `{hours_silent:.1f} hours ago`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"*Possible causes:*\n"
            f"  1. TradingView alert EXPIRED\n"
            f"  2. Market closed (weekend/holiday)\n"
            f"  3. No qualifying signal (normal)\n"
            f"  4. Webhook URL / SSL issue\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"*Actions:*\n"
            f"  • Check TradingView → Alerts tab\n"
            f"  • Run: `curl https://YOUR_DOMAIN/health`\n"
            f"  • Check logs: `docker logs smartgold-ai-bridge --tail 20`"
        )

        return await self._send_telegram(text)

    # ══════════════════════════════════════════════════════════════════
    # Heartbeat background loop
    # ══════════════════════════════════════════════════════════════════

    def mark_signal_received(self) -> None:
        """Call this whenever ANY signal arrives (even if skipped)."""
        self._last_signal_ts = time.time()

    async def _heartbeat_loop(self, silence_hours: float) -> None:
        """Background loop: check every 30 min if we've gone too long without a signal."""
        check_interval = 30 * 60  # check every 30 minutes
        silence_threshold = silence_hours * 3600
        # Track if we already alerted for this silence window (avoid spam)
        alerted_for_current_silence = False

        try:
            while True:
                await asyncio.sleep(check_interval)
                elapsed = time.time() - self._last_signal_ts
                if elapsed > silence_threshold:
                    if not alerted_for_current_silence:
                        hours = elapsed / 3600.0
                        await self.notify_silence(hours)
                        alerted_for_current_silence = True
                        logger.info(
                            "Monitor: silence alert sent ({:.1f}h without signal)",
                            hours,
                        )
                else:
                    # Reset alert flag when a new signal arrives
                    alerted_for_current_silence = False
        except asyncio.CancelledError:
            logger.info("Monitor heartbeat loop cancelled")
            raise

    def start_heartbeat(self, silence_hours: float = 6.0) -> None:
        """Start the background heartbeat checker.

        Args:
            silence_hours: Alert if no signal received in this many hours.
                           Default 6h — conservative for H1 timeframe.
                           Set via MONITOR_SILENCE_HOURS env var.
        """
        if self._heartbeat_task is not None:
            return  # already running
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(silence_hours),
            name="monitor_heartbeat",
        )
        logger.info(
            "Monitor heartbeat started (alert after {}h silence)", silence_hours
        )

    async def stop_heartbeat(self) -> None:
        """Stop the heartbeat loop gracefully."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._heartbeat_task = None


# ══════════════════════════════════════════════════════════════════════
# Module-level singleton (same pattern as TelegramNotifier)
# ══════════════════════════════════════════════════════════════════════
_monitor: Monitor | None = None


def get_monitor(settings: Settings | None = None) -> Monitor:
    """Return a singleton Monitor instance."""
    global _monitor
    if _monitor is None:
        _monitor = Monitor(settings)
    return _monitor


__all__ = ["Monitor", "get_monitor"]
