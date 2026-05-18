"""S1 - Early momentum sniper for fresh Solana SPL launches.

Entry filter is intentionally strict: small minted age, real liquidity floor,
positive 5-minute price action, and elevated volume relative to liquidity.
These filters cut out the bulk of obvious rug/dump launches at the cost of
missing some monster pumps that never showed early signal.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import settings
from ..data_sources import TokenSnapshot
from .base import Position


@dataclass
class MomentumSniperS1:
    name: str = "S1"
    allocation: float = 0.20

    min_liquidity_usd: float = 5_000.0
    max_liquidity_usd: float = 250_000.0  # if it's this big it's not "early"
    min_vol_to_liq: float = 0.25  # 5m volume / liquidity
    min_price_change_5m: float = 5.0  # already moving up
    max_price_change_5m: float = 250.0  # but not parabolic-already
    max_age_minutes: int = 30

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
            if snap.age_minutes > self.max_age_minutes:
                continue
            if snap.volume_usd_5m <= 0 or snap.liquidity_usd <= 0:
                continue
            if snap.volume_usd_5m / snap.liquidity_usd < self.min_vol_to_liq:
                continue
            if not (self.min_price_change_5m <= snap.price_change_5m <= self.max_price_change_5m):
                continue
            out.append(snap)
        # Best-first: pick highest volume-to-liquidity ratio.
        out.sort(key=lambda s: s.volume_usd_5m / max(s.liquidity_usd, 1.0), reverse=True)
        # Cap to one new S1 position per tick to avoid stacking risk.
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
                # If we lost the feed, exit on next tick to free capital.
                if tick - pos.opened_at_tick > 30:
                    out.append((pos, "feed_lost"))
                continue
            mult = snap.price_usd / pos.entry_price if pos.entry_price > 0 else 1.0
            pos.max_seen_mult = max(pos.max_seen_mult, mult)
            # Trailing stop: once we've seen >1.5x, sell if we give back to 75% of peak.
            if pos.max_seen_mult >= 1.5 and mult <= pos.max_seen_mult * 0.75:
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
