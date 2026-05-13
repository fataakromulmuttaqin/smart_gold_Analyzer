"""Safety guards — pre-execution checks that can veto or downgrade signals.

The guard chain runs *after* the LLM decision but *before* broker execution
and Telegram notification. Each guard can:
  • PASS   — no objection, continue.
  • BLOCK  — hard veto; signal is dropped entirely.
  • REDUCE — downgrade lot size / confidence (e.g. during news windows).

Guards are cheap, synchronous, and stateless — they should never call
external APIs or do I/O. They inspect the decision + context and return
a verdict instantly.
"""

from app.guards.chain import GuardChain, GuardVerdict  # noqa: F401
from app.guards.guards import (  # noqa: F401
    DrawdownGuard,
    MaxATRGuard,
    MaxDailyTradesGuard,
    NewsBlackoutGuard,
    SpreadGuard,
)

__all__ = [
    "GuardChain",
    "GuardVerdict",
    "DrawdownGuard",
    "MaxATRGuard",
    "MaxDailyTradesGuard",
    "NewsBlackoutGuard",
    "SpreadGuard",
]
