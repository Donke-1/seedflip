"""Shared strategy interfaces."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..data_sources import TokenSnapshot


@dataclass
class Position:
    symbol: str
    address: str
    entry_price: float
    size_usd: float
    opened_at_tick: int
    strategy: str
    max_seen_mult: float = 1.0  # for trailing-stop logic
    take_profit_mult: float = 2.0
    stop_loss_pct: float = 0.4
    max_age_ticks: int = 60 * 6  # 6 hours at 1-min ticks


@dataclass
class StrategyDecision:
    enter: list[TokenSnapshot] = field(default_factory=list)
    exits: list[tuple[Position, str]] = field(default_factory=list)  # (position, reason)


class Strategy(Protocol):
    name: str
    allocation: float  # fraction of bankroll this strategy is willing to deploy

    def consider_entries(
        self,
        candidates: list[TokenSnapshot],
        open_positions: list[Position],
        bankroll_usd: float,
    ) -> list[TokenSnapshot]:
        """Return the subset of candidates to enter this tick."""

    def consider_exits(
        self,
        positions: list[Position],
        snapshots_by_address: dict[str, TokenSnapshot],
        tick: int,
    ) -> list[tuple[Position, str]]:
        """Return (position, reason) pairs to close this tick."""
