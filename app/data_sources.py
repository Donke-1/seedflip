"""Lightweight clients for public crypto data APIs.

We use only public endpoints (no API keys) so the bot is fully deployable
without secrets. All clients are tolerant to upstream errors and return
empty results rather than raising; the runner treats absent data as "no
opportunity this tick".
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


@dataclass
class TokenSnapshot:
    """Normalized view of a Solana token from DexScreener-style payloads."""

    address: str
    symbol: str
    price_usd: float
    liquidity_usd: float
    volume_usd_5m: float
    volume_usd_1h: float
    fdv_usd: float
    price_change_5m: float
    price_change_1h: float
    pair_created_at_ms: int  # 0 if unknown
    chain: str = "solana"

    @property
    def age_minutes(self) -> float:
        if self.pair_created_at_ms <= 0:
            return 1e9
        import time

        return max(0.0, (time.time() * 1000 - self.pair_created_at_ms) / 60_000.0)


class DexScreenerClient:
    def __init__(self, base_url: str | None = None, timeout: float = 6.0):
        self.base_url = base_url or settings.dexscreener_base_url
        self.timeout = timeout

    async def _get(self, path: str) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(f"{self.base_url}{path}")
                if r.status_code != 200:
                    log.warning("DexScreener %s -> %s", path, r.status_code)
                    return None
                return r.json()
        except Exception as exc:  # noqa: BLE001 - network failures are expected
            log.warning("DexScreener %s failed: %s", path, exc)
            return None

    @staticmethod
    def _to_snapshot(pair: dict[str, Any]) -> TokenSnapshot | None:
        try:
            base = pair.get("baseToken") or {}
            liquidity = pair.get("liquidity") or {}
            volume = pair.get("volume") or {}
            price_change = pair.get("priceChange") or {}
            return TokenSnapshot(
                address=str(base.get("address", "")),
                symbol=str(base.get("symbol", "?")),
                price_usd=float(pair.get("priceUsd") or 0.0),
                liquidity_usd=float(liquidity.get("usd") or 0.0),
                volume_usd_5m=float(volume.get("m5") or 0.0),
                volume_usd_1h=float(volume.get("h1") or 0.0),
                fdv_usd=float(pair.get("fdv") or 0.0),
                price_change_5m=float(price_change.get("m5") or 0.0),
                price_change_1h=float(price_change.get("h1") or 0.0),
                pair_created_at_ms=int(pair.get("pairCreatedAt") or 0),
                chain=str(pair.get("chainId") or "solana"),
            )
        except (ValueError, TypeError) as exc:
            log.debug("Bad pair payload: %s", exc)
            return None

    async def search_solana(self, query: str = "SOL") -> list[TokenSnapshot]:
        data = await self._get(f"/latest/dex/search?q={query}")
        if not data or "pairs" not in data:
            return []
        out: list[TokenSnapshot] = []
        for pair in data["pairs"]:
            if pair.get("chainId") != "solana":
                continue
            snap = self._to_snapshot(pair)
            if snap and snap.liquidity_usd > 0 and snap.price_usd > 0:
                out.append(snap)
        return out

    async def pair(self, pair_address: str, chain: str = "solana") -> TokenSnapshot | None:
        data = await self._get(f"/latest/dex/pairs/{chain}/{pair_address}")
        if not data or not data.get("pair"):
            return None
        return self._to_snapshot(data["pair"])

    async def majors_solana(self) -> list[TokenSnapshot]:
        """SOL/USDC-style high-liquidity pairs we can scalp on (strategy S3)."""
        snaps = await self.search_solana("SOL/USDC")
        snaps.sort(key=lambda s: s.liquidity_usd, reverse=True)
        return snaps[:5]


# Convenience for callers that don't want async
def search_solana_sync(query: str = "SOL") -> list[TokenSnapshot]:
    return asyncio.run(DexScreenerClient().search_solana(query))
