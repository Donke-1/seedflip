"""Paper/live trade runner.

Paper mode:
- Polls DexScreener every N seconds for Solana candidate tokens.
- Feeds those snapshots to S1/S2 (memecoin sniper strategies).
- Feeds SOL/USDC majors to S3 (mean-reversion scalp).
- Opens/closes simulated positions, persists trades + equity snapshots.

Live mode:
- HARD GATED. The runner refuses to enter live mode unless:
    1) settings.live_trading_armed is True (env var) AND
    2) BotState.mode == "live" (only switchable via /control/go-live POST with a confirmation token).
- When both are set, entries call Jupiter to actually submit USDC -> SPL token
  swaps signed by the on-disk keypair. Exits do the reverse. If a swap fails
  to confirm we skip the trade and keep bankroll intact — no synthetic
  position is recorded. All real txids are persisted on the Trade row.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import select

from . import wallet as wallet_mod
from .config import settings
from .data_sources import DexScreenerClient, TokenSnapshot
from .db import session_scope
from .jupiter import JupiterClient
from .models import BotState, EquitySnapshot, Trade
from .strategies import MajorsScalpS3, MigrationSniperS2, MomentumSniperS1
from .strategies.base import Position
from .strategy_config import best_known_params

log = logging.getLogger(__name__)


# Single global runner instance; FastAPI lifespan owns its lifecycle.
RUNNER: Optional["Runner"] = None


@dataclass
class RunnerSettings:
    tick_seconds: float = 30.0
    paper_only_default: bool = True
    poll_concurrency: int = 4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Runner:
    def __init__(self, cfg: RunnerSettings | None = None):
        self.cfg = cfg or RunnerSettings()
        self.dex = DexScreenerClient()
        # Backtest-tuned config defines per-strategy allocation + sizing.
        bp = best_known_params()
        self._bp = bp
        self.s1 = MomentumSniperS1()
        self.s2 = MigrationSniperS2()
        self.s3 = MajorsScalpS3()
        self.s1.allocation = bp.alloc_s1
        self.s2.allocation = bp.alloc_s2
        self.s3.allocation = bp.alloc_s3
        self._open_positions: list[Position] = []
        self._tick: int = 0
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        # One-time go-live confirmation token; required to flip to "live" mode.
        self.go_live_token = secrets.token_urlsafe(16)
        log.info("Runner go-live confirmation token: %s", self.go_live_token)

    # ---- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run_loop(), name="seedflip_runner")
            log.info("Runner started")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        log.info("Runner stopped")

    # ---- main loop -----------------------------------------------------------

    async def _run_loop(self) -> None:
        try:
            await self._ensure_state()
            while not self._stop_event.is_set():
                try:
                    await self._tick_once()
                except Exception:  # noqa: BLE001
                    log.exception("tick failed")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.cfg.tick_seconds)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    async def _ensure_state(self) -> None:
        # Initialize BotState row if not present.
        with session_scope() as s:
            existing = s.exec(select(BotState)).first()
            if existing is None:
                s.add(BotState(id=1, mode="paper", bankroll_usd=settings.seed_capital_usd,
                               bankroll_start_usd=settings.seed_capital_usd,
                               bankroll_day_start_usd=settings.seed_capital_usd))

    async def _tick_once(self) -> None:
        self._tick += 1
        # Fetch market data with reasonable parallelism.
        memes, majors = await asyncio.gather(
            self.dex.search_solana("pump"),  # broad search; pump.fun-style tokens often surface here
            self.dex.majors_solana(),
        )
        # Also pull a generic SOL search to widen the meme candidate pool.
        if len(memes) < 5:
            more = await self.dex.search_solana("SOL")
            memes = memes + more

        snapshots_by_address: dict[str, TokenSnapshot] = {}
        for snap in memes + majors:
            snapshots_by_address[snap.address] = snap

        with session_scope() as session:
            state = session.exec(select(BotState)).first()
            if state is None:
                return
            if state.mode == "halted":
                state.last_tick_at = _utcnow()
                return

            # Roll the day at UTC midnight.
            self._maybe_roll_day(state)

            # Halt conditions.
            equity = self._equity_estimate(state, snapshots_by_address)
            if equity < settings.dust_floor_usd:
                state.mode = "halted"
                state.halted_reason = "dust"
                state.last_tick_at = _utcnow()
                session.add(state)
                return
            if state.bankroll_day_start_usd > 0 and equity < state.bankroll_day_start_usd * (1 - settings.daily_loss_cutoff_pct):
                state.mode = "halted"
                state.halted_reason = "daily_loss_cutoff"
                state.last_tick_at = _utcnow()
                session.add(state)
                return

            # Close due / failing positions.
            await self._maybe_close_positions(session, snapshots_by_address, state)

            # Open new ones if we have spare capital.
            await self._maybe_open_positions(session, memes, majors, state, snapshots_by_address)

            state.last_tick_at = _utcnow()
            session.add(state)

            # Equity snapshot every ~5 ticks (~2.5 min) for the chart.
            if self._tick % 5 == 0:
                snap = EquitySnapshot(equity_usd=self._equity_estimate(state, snapshots_by_address), mode=state.mode)
                session.add(snap)

    # ---- helpers -------------------------------------------------------------

    def _maybe_roll_day(self, state: BotState) -> None:
        now = _utcnow()
        if state.day_start_at.tzinfo is None:
            state.day_start_at = state.day_start_at.replace(tzinfo=timezone.utc)
        if now - state.day_start_at >= timedelta(days=1):
            state.day_start_at = now
            state.bankroll_day_start_usd = state.bankroll_usd + sum(p.size_usd for p in self._open_positions)

    def _equity_estimate(self, state: BotState, snaps: dict[str, TokenSnapshot]) -> float:
        equity = state.bankroll_usd
        for p in self._open_positions:
            snap = snaps.get(p.address)
            if snap is None:
                equity += p.size_usd  # unknown price -> conservatively count book value
            else:
                mult = (snap.price_usd / p.entry_price) if p.entry_price > 0 else 1.0
                equity += p.size_usd * mult
        return equity

    def _is_live(self, state: BotState) -> bool:
        """Real Jupiter trading requires BOTH the env arming flag AND state.mode."""
        return bool(
            state.mode == "live"
            and settings.live_trading_armed
            and wallet_mod.WALLET is not None
        )

    async def _maybe_close_positions(self, session, snaps: dict[str, TokenSnapshot], state: BotState) -> None:
        # Ask each strategy for exits.
        exits_s1 = self.s1.consider_exits(self._open_positions, snaps, self._tick)
        exits_s2 = self.s2.consider_exits(self._open_positions, snaps, self._tick)
        exits_s3 = self.s3.consider_exits(self._open_positions, snaps, self._tick)
        all_exits = exits_s1 + exits_s2 + exits_s3
        if not all_exits:
            return

        live = self._is_live(state)
        to_remove: set[int] = set()
        for pos, reason in all_exits:
            snap = snaps.get(pos.address)
            exit_price = snap.price_usd if snap else pos.entry_price

            if live and pos.token_units > 0:
                proceeds, exit_txid, confirmed = await self._execute_close_live(pos)
                if not confirmed:
                    # On-chain sell failed (rate limit, no route, slippage too big).
                    # Leave the position open and retry next tick. Do NOT realize a
                    # synthetic exit because we still hold the tokens.
                    log.warning(
                        "live sell failed for %s (%s); will retry next tick",
                        pos.symbol, pos.address,
                    )
                    continue
                pos.exit_txid = exit_txid
                pnl = proceeds - pos.size_usd
                mult = (proceeds / pos.size_usd) if pos.size_usd > 0 else 1.0
            else:
                mult = (exit_price / pos.entry_price) if pos.entry_price > 0 else 1.0
                proceeds = pos.size_usd * mult
                pnl = proceeds - pos.size_usd

            state.bankroll_usd += proceeds
            session.add(Trade(
                opened_at=_utcnow() - timedelta(minutes=max(0, self._tick - pos.opened_at_tick)),
                closed_at=_utcnow(),
                strategy=pos.strategy,
                symbol=pos.symbol,
                side="long",
                size_usd=pos.size_usd,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                pnl_usd=pnl,
                pnl_pct=(mult - 1.0),
                exit_reason=reason,
                mode=state.mode,
                run_id=pos.entry_txid or None,
            ))
            to_remove.add(id(pos))
        self._open_positions = [p for p in self._open_positions if id(p) not in to_remove]

    async def _execute_close_live(self, pos: Position) -> tuple[float, str, bool]:
        """Sell `pos.token_units` of `pos.address` for USDC via Jupiter.

        Returns (proceeds_usd, txid, confirmed). On failure proceeds=0.
        """
        assert wallet_mod.WALLET is not None  # narrowed by _is_live
        jup = JupiterClient(wallet_mod.WALLET)
        try:
            res = await jup.sell_token_to_usdc(pos.address, pos.token_units)
        except Exception:  # noqa: BLE001 — Jupiter quote / RPC may fail; treat as a missed exit
            log.exception("Jupiter sell failed for %s", pos.symbol)
            return 0.0, "", False
        finally:
            await jup.aclose()
        if not res.confirmed:
            return 0.0, res.txid or "", False
        proceeds_usd = res.output_amount / 1_000_000  # USDC has 6 decimals
        return proceeds_usd, res.txid, True

    async def _maybe_open_positions(
        self, session, memes: list[TokenSnapshot], majors: list[TokenSnapshot],
        state: BotState, snaps: dict[str, TokenSnapshot],
    ) -> None:
        bp = self._bp
        equity = self._equity_estimate(state, snaps)
        if equity >= bp.target_usd * bp.target_lock_pct:
            return  # target hit, lock in

        deployed_by_strat = {"S1": 0.0, "S2": 0.0, "S3": 0.0}
        for p in self._open_positions:
            deployed_by_strat[p.strategy] += p.size_usd

        candidates_s1 = self.s1.consider_entries(memes, self._open_positions, state.bankroll_usd)
        candidates_s2 = self.s2.consider_entries(memes, self._open_positions, state.bankroll_usd)
        candidates_s3 = self.s3.consider_entries(majors, self._open_positions, state.bankroll_usd)

        live = self._is_live(state)

        for strat_name, strat, candidates, alloc, trade_pct, tp_mult, sl_pct in [
            ("S1", self.s1, candidates_s1, bp.alloc_s1, bp.s1_trade_pct, bp.outcomes.s1.tp_mid, 0.40),
            ("S2", self.s2, candidates_s2, bp.alloc_s2, bp.s2_trade_pct, bp.outcomes.s2.tp_mid, 0.50),
            ("S3", self.s3, candidates_s3, bp.alloc_s3, bp.s3_trade_pct, 1.0 + bp.outcomes.s3.target_profit_pct, bp.outcomes.s3.stop_loss_pct),
        ]:
            for snap in candidates:
                cap_for_strat = max(0.0, alloc * equity - deployed_by_strat[strat_name])
                size_pct = trade_pct
                if bp.adaptive_sizing:
                    if equity <= bp.adaptive_low_mult * bp.seed_usd:
                        size_pct *= bp.adaptive_low_boost
                    elif equity >= bp.adaptive_high_mult * bp.target_usd:
                        size_pct *= bp.adaptive_high_cut
                per_trade = min(state.bankroll_usd * size_pct, state.bankroll_usd * bp.max_trade_pct, cap_for_strat)
                if per_trade < 0.10:
                    continue

                token_units = 0
                entry_txid = ""
                effective_entry_price = snap.price_usd
                effective_size_usd = per_trade

                if live:
                    res = await self._execute_open_live(snap.address, per_trade)
                    if res is None:
                        # Swap didn't confirm — bankroll untouched, skip.
                        continue
                    token_units = res["token_units"]
                    entry_txid = res["txid"]
                    effective_size_usd = res["spent_usdc"]
                    # Derive an on-chain effective entry price (USDC per token).
                    if token_units > 0:
                        try:
                            decimals = await wallet_mod.WALLET.get_token_decimals(snap.address)  # type: ignore[union-attr]
                            tokens_whole = token_units / (10 ** decimals)
                            if tokens_whole > 0:
                                effective_entry_price = effective_size_usd / tokens_whole
                        except Exception:  # noqa: BLE001
                            log.warning("Could not derive entry price for %s, falling back to feed", snap.symbol)

                state.bankroll_usd -= effective_size_usd
                deployed_by_strat[strat_name] += effective_size_usd
                pos = Position(
                    symbol=snap.symbol, address=snap.address, entry_price=effective_entry_price,
                    size_usd=effective_size_usd, opened_at_tick=self._tick, strategy=strat_name,
                    take_profit_mult=tp_mult, stop_loss_pct=sl_pct,
                    token_units=token_units, entry_txid=entry_txid,
                )
                self._open_positions.append(pos)
                session.add(Trade(
                    strategy=strat_name, symbol=snap.symbol, side="long",
                    size_usd=effective_size_usd, entry_price=effective_entry_price, mode=state.mode,
                    run_id=entry_txid or None,
                ))

    async def _execute_open_live(self, mint: str, usd_amount: float) -> Optional[dict]:
        """Buy `mint` with `usd_amount` USDC via Jupiter. Returns None if swap fails.

        Returns dict with keys: token_units (int), txid (str), spent_usdc (float)
        """
        assert wallet_mod.WALLET is not None  # _is_live narrowed
        usdc_units = int(round(usd_amount * 1_000_000))
        if usdc_units <= 0:
            return None
        jup = JupiterClient(wallet_mod.WALLET)
        try:
            res = await jup.buy_usdc_to_token(mint, usdc_units)
        except Exception:  # noqa: BLE001
            log.exception("Jupiter buy failed for %s", mint)
            return None
        finally:
            await jup.aclose()
        if not res.confirmed or res.output_amount <= 0:
            log.warning("buy unconfirmed for %s txid=%s", mint, res.txid)
            return None
        spent_usdc = res.input_amount / 1_000_000
        return {"token_units": int(res.output_amount), "txid": res.txid, "spent_usdc": spent_usdc}

    # ---- introspection used by API -----------------------------------------

    def open_positions_snapshot(self) -> list[dict]:
        return [
            {
                "symbol": p.symbol,
                "address": p.address,
                "strategy": p.strategy,
                "size_usd": p.size_usd,
                "entry_price": p.entry_price,
                "opened_at_tick": p.opened_at_tick,
                "max_seen_mult": p.max_seen_mult,
            }
            for p in self._open_positions
        ]
