"""Safety guards that protect the trading bridge from excessive risk.

Guards are checked BEFORE broker execution. If any guard trips, the
decision is downgraded to ``skip`` with a clear reason — the signal is
still logged for audit but no order is placed.

Available guards:
  * :class:`WeekendGuard`         — block trading on weekends (Sat/Sun UTC)
  * :class:`ConsecutiveLossGuard` — circuit breaker after N consecutive losses
  * :class:`MaxDailyTradesGuard`  — cap the number of trades per calendar day
  * :class:`DrawdownGuard`        — halt trading if daily drawdown exceeds threshold
  * :class:`NewsBlackoutGuard`    — block during high-impact news windows

All guards implement the :class:`Guard` protocol and are composed via
:class:`GuardChain`.
"""

from app.guards.chain import Guard, GuardChain, GuardResult
from app.guards.guards import (
    ConsecutiveLossGuard,
    DrawdownGuard,
    MaxDailyTradesGuard,
    NewsBlackoutGuard,
    WeekendGuard,
)

__all__ = [
    "Guard",
    "GuardChain",
    "GuardResult",
    "WeekendGuard",
    "ConsecutiveLossGuard",
    "MaxDailyTradesGuard",
    "DrawdownGuard",
    "NewsBlackoutGuard",
]
