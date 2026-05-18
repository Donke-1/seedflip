"""S3 - SOL/USDC mean-reversion scalp on the deepest pool.

This is the "anchor" strategy. It targets a tiny edge (~0.5-1% per scalp) on
the most liquid Solana pair so that drawdowns are bounded and we don't bleed
to zero on days where no good memecoin signal appears. Expected to contribute
small-but-consistent positive expectancy and to provide diversification
relative to S1/S2.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from ..config import settings
from ..data_sources import TokenSnapshot
from .base import Position


@dataclass
class MajorsScalpS3:
    name: str = "S3"
    allocation: float = 0.70
    band_pct: float = 0.015
    target_profit_pct: float = 0.008
    max_concurrent: int = 3
    ewma_alpha: float = 0.08
    _ewma_by_addr: dict[str, float] = field(default_factory=dict)
    _last_prices: dict[str, deque[float]] = field(default_factory=dict)

    def _update_ewma(self, snap: TokenSnapshot) -> float:
        prev = self._ewma_by_addr.get(snap.address)
        if prev is None:
            self._ewma_by_addr[snap.address] = snap.price_usd
            return snap.price_usd
        new_ewma = self.ewma_alpha * snap.price_usd + (1 - self.ewma_alpha) * prev
        self._ewma_by_addr[snap.address] = new_ewma
        # Track recent prices to require some price variation before scalping
        q = self._last_prices.setdefault(snap.address, deque(maxlen=20))
        q.append(snap.price_usd)
        return new_ewma

    def consider_entries(
        self,
        candidates: list[TokenSnapshot],
        open_positions: list[Position],
        bankroll_usd: float,
    ) -> list[TokenSnapshot]:
        if bankroll_usd <= settings.dust_floor_usd:
            return []
        s3_held = [p for p in open_positions if p.strategy == self.name]
        if len(s3_held) >= self.max_concurrent:
            return []
        out: list[TokenSnapshot] = []
        for snap in candidates:
            ewma = self._update_ewma(snap)
            if ewma <= 0:
                continue
            # Require we've seen enough prices to estimate the band reliably.
            q = self._last_prices.get(snap.address)
            if not q or len(q) < 5:
                continue
            # Enter long only when current price is meaningfully below the EWMA.
            if snap.price_usd <= ewma * (1.0 - self.band_pct):
                if not any(p.address == snap.address for p in open_positions):
                    out.append(snap)
        return out[: max(0, self.max_concurrent - len(s3_held))]

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
                if tick - pos.opened_at_tick > 30:
                    out.append((pos, "feed_lost"))
                continue
            mult = snap.price_usd / pos.entry_price if pos.entry_price > 0 else 1.0
            if mult >= 1.0 + self.target_profit_pct:
                out.append((pos, "tp"))
                continue
            # Tight stop - scalps must die fast.
            if mult <= 1.0 - (self.target_profit_pct * 2.5):
                out.append((pos, "sl"))
                continue
            if tick - pos.opened_at_tick >= 60:  # 60 ticks ~ 1 hour
                out.append((pos, "timeout"))
        return out
