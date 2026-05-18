"""S2 - Pump.fun -> Raydium migration sniper.

Tokens that "graduate" from pump.fun's bonding curve to a real Raydium LP
historically see a brief liquidity-bootstrap-induced price pop. We approximate
detection without a private indexer by looking for: very recent Raydium
listing, sub-day age, and a discrete jump in liquidity relative to age.

This is the "lottery tier" of the system: lowest allocation, biggest upside
target. Most picks lose. Occasional picks 3-10x and that's where the fat tail
lives.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import settings
from ..data_sources import TokenSnapshot
from .base import Position


@dataclass
class MigrationSniperS2:
    name: str = "S2"
    allocation: float = 0.10

    min_liquidity_usd: float = 30_000.0
    max_liquidity_usd: float = 400_000.0
    min_age_minutes: int = 1
    max_age_minutes: int = 360  # 6 hours
    min_fdv_usd: float = 50_000.0
    min_price_change_1h: float = 30.0  # rapid early appreciation

    def consider_entries(
        self,
        candidates: list[TokenSnapshot],
        open_positions: list[Position],
        bankroll_usd: float,
    ) -> list[TokenSnapshot]:
        if bankroll_usd <= settings.dust_floor_usd:
            return []
        held = {p.address for p in open_positions}
        out: list[TokenSnapshot] = []
        for snap in candidates:
            if snap.address in held:
                continue
            if not (self.min_liquidity_usd <= snap.liquidity_usd <= self.max_liquidity_usd):
                continue
            if not (self.min_age_minutes <= snap.age_minutes <= self.max_age_minutes):
                continue
            if snap.fdv_usd < self.min_fdv_usd:
                continue
            if snap.price_change_1h < self.min_price_change_1h:
                continue
            out.append(snap)
        out.sort(key=lambda s: s.price_change_1h, reverse=True)
        return out[:1]

    def consider_exits(
        self,
        positions: list[Position],
        snapshots_by_address: dict[str, TokenSnapshot],
        tick: int,
    ) -> list[tuple[Position, str]]:
        out: list[tuple[Position, str]] = []
        for pos in positions:
            if pos.strategy != self.name:
                continue
            snap = snapshots_by_address.get(pos.address)
            if snap is None:
                if tick - pos.opened_at_tick > 60:
                    out.append((pos, "feed_lost"))
                continue
            mult = snap.price_usd / pos.entry_price if pos.entry_price > 0 else 1.0
            pos.max_seen_mult = max(pos.max_seen_mult, mult)
            # Looser trailing stop than S1 - we want to let winners run further.
            if pos.max_seen_mult >= 2.0 and mult <= pos.max_seen_mult * 0.7:
                out.append((pos, "trailing_stop"))
                continue
            if mult >= pos.take_profit_mult:
                out.append((pos, "tp"))
                continue
            if mult <= (1.0 - pos.stop_loss_pct):
                out.append((pos, "sl"))
                continue
            if tick - pos.opened_at_tick >= pos.max_age_ticks:
                out.append((pos, "timeout"))
        return out
