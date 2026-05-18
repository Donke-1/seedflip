"""Monte Carlo backtester for the SeedFlip system.

This is the most important module: it is what tells us *honestly* whether the
strategy ensemble has any meaningful probability of hitting the $100 target
from $10 in 7 days, given the outcome distributions in ``simulator.py``.

Design:
- Event-driven: each strategy emits opportunity events as a Poisson process.
- Per sim: walk forward through events, open positions when allocation permits,
  close positions as their pre-sampled durations elapse, apply outcomes to
  bankroll.
- Halt conditions: bankroll < dust floor, or daily loss > cutoff.
- Return: per-sim final bankroll + sample trajectory + summary statistics.
"""
from __future__ import annotations

import heapq
import json
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from .config import settings
from .simulator import (
    ArrivalParams,
    StrategyOutcomeParams,
    sample_s1_outcome,
    sample_s2_outcome,
    sample_s3_outcome,
    params_to_dict,
)


@dataclass
class BacktestParams:
    seed_usd: float = 10.0
    target_usd: float = 100.0
    horizon_days: int = 7
    n_simulations: int = 2000
    seed_rng: int = 42

    # Tier allocations (fraction of bankroll each strategy is willing to deploy
    # across all of its concurrent open positions combined).
    alloc_s1: float = 0.20
    alloc_s2: float = 0.10
    alloc_s3: float = 0.70

    # Per-trade sizing.
    s1_trade_pct: float = 0.12  # fraction of bankroll
    s2_trade_pct: float = 0.10
    s3_trade_pct: float = 0.18

    # Adaptive sizing: scale per-trade fraction by progress to target.
    # When bankroll is between [adaptive_low_mult * seed, adaptive_high_mult * target],
    # we keep sizing flat. Below the low mark we go MORE aggressive (escape dust),
    # above the high mark we LOCK in by sizing down.
    adaptive_sizing: bool = False
    adaptive_low_mult: float = 1.0   # at seed
    adaptive_high_mult: float = 0.6  # at 60% of target
    adaptive_low_boost: float = 1.5  # 1.5x sizing when at or below seed
    adaptive_high_cut: float = 0.4   # 0.4x sizing when at or above high mark
    target_lock_pct: float = 0.95    # once we're >= target_lock_pct * target, no new trades

    # Risk controls.
    max_trade_pct: float = 0.40  # cap so adaptive boost can't break the bank in one trade
    daily_loss_cutoff_pct: float = 0.40
    dust_floor_usd: float = 0.50

    outcomes: StrategyOutcomeParams = field(default_factory=StrategyOutcomeParams)

    def horizon_minutes(self) -> int:
        return self.horizon_days * 24 * 60


@dataclass
class BacktestSummary:
    n_simulations: int
    seed_usd: float
    target_usd: float
    horizon_days: int

    median_final: float
    mean_final: float
    p5_final: float
    p25_final: float
    p75_final: float
    p95_final: float
    max_final: float

    p_hit_target: float
    p_hit_2x: float
    p_hit_3x: float
    p_hit_5x: float
    p_near_zero: float

    sample_trajectories: list[list[tuple[int, float]]]  # [(minute, equity)] per sample
    histogram_bins: list[float]
    histogram_counts: list[int]
    params_json: str


def _arrivals(rng: np.random.Generator, rate_per_day: float, horizon_min: int,
              regime_sigma: float) -> np.ndarray:
    """Generate event arrival times in minutes via inhomogeneous Poisson.

    We model a multiplicative day-by-day regime so some days are flat-out
    dead and others are gold mines, which matches lived experience on
    Solana memecoin markets.
    """
    days = max(1, horizon_min // (24 * 60))
    daily_factors = np.exp(rng.normal(0.0, regime_sigma, size=days + 1))
    arrivals: list[float] = []
    for d in range(days + 1):
        day_start = d * 24 * 60
        day_end = min((d + 1) * 24 * 60, horizon_min)
        if day_end <= day_start:
            continue
        lam = rate_per_day * daily_factors[d] / (24 * 60)  # per minute
        if lam <= 0:
            continue
        n_expected = (day_end - day_start) * lam
        n_actual = rng.poisson(n_expected)
        if n_actual > 0:
            times = rng.uniform(day_start, day_end, size=n_actual)
            arrivals.extend(times.tolist())
    arrivals.sort()
    return np.array(arrivals, dtype=float)


def _run_one_sim(params: BacktestParams, rng: np.random.Generator,
                 trajectory_resolution: int = 30) -> tuple[float, list[tuple[int, float]], list[dict]]:
    horizon_min = params.horizon_minutes()
    bankroll = float(params.seed_usd)
    bankroll_day_start = bankroll

    # Pre-generate arrivals per strategy.
    s1_times = _arrivals(rng, params.outcomes.arrivals.s1_per_day, horizon_min,
                         params.outcomes.arrivals.daily_regime_sigma)
    s2_times = _arrivals(rng, params.outcomes.arrivals.s2_per_day, horizon_min,
                         params.outcomes.arrivals.daily_regime_sigma)
    s3_times = _arrivals(rng, params.outcomes.arrivals.s3_per_day, horizon_min,
                         params.outcomes.arrivals.daily_regime_sigma)

    events: list[tuple[float, str]] = (
        [(t, "S1") for t in s1_times] +
        [(t, "S2") for t in s2_times] +
        [(t, "S3") for t in s3_times]
    )
    events.sort()

    # Open positions heap: (close_min, strategy, locked_usd, outcome_mult)
    open_heap: list[tuple[float, str, float, float]] = []
    deployed_by_strat: dict[str, float] = {"S1": 0.0, "S2": 0.0, "S3": 0.0}
    alloc_by_strat = {"S1": params.alloc_s1, "S2": params.alloc_s2, "S3": params.alloc_s3}
    trade_pct_by_strat = {"S1": params.s1_trade_pct, "S2": params.s2_trade_pct, "S3": params.s3_trade_pct}

    trajectory: list[tuple[int, float]] = [(0, bankroll)]
    last_traj_min = 0
    halted_reason = ""
    trades_log: list[dict] = []

    def _close_due(now_min: float) -> None:
        nonlocal bankroll
        while open_heap and open_heap[0][0] <= now_min:
            close_min, strat, locked, mult = heapq.heappop(open_heap)
            proceeds = locked * mult
            bankroll += proceeds
            deployed_by_strat[strat] -= locked
            trades_log.append({
                "strategy": strat,
                "size_usd": locked,
                "exit_mult": mult,
                "pnl_usd": proceeds - locked,
                "close_min": close_min,
            })

    def _record_trajectory(now_min: float) -> None:
        nonlocal last_traj_min
        if now_min - last_traj_min >= trajectory_resolution:
            equity = bankroll + sum(loc for _, _, loc, _ in open_heap)
            trajectory.append((int(now_min), equity))
            last_traj_min = now_min

    day_idx = 0
    for ev_time, strat in events:
        # Close any positions due before this event.
        _close_due(ev_time)
        _record_trajectory(ev_time)

        # Day rollover for daily loss cutoff.
        new_day_idx = int(ev_time // (24 * 60))
        if new_day_idx != day_idx:
            day_idx = new_day_idx
            bankroll_day_start = bankroll + sum(loc for _, _, loc, _ in open_heap)

        # Halt: bankroll dusted.
        if bankroll <= params.dust_floor_usd and not open_heap:
            halted_reason = "dust"
            break

        # Halt: daily loss cutoff (only checks at event times).
        equity_now = bankroll + sum(loc for _, _, loc, _ in open_heap)
        if bankroll_day_start > 0 and equity_now < bankroll_day_start * (1 - params.daily_loss_cutoff_pct):
            halted_reason = "daily_loss"
            # Skip trading for the rest of this day.
            continue

        if bankroll < params.dust_floor_usd:
            continue

        alloc = alloc_by_strat[strat]
        equity = bankroll + sum(loc for _, _, loc, _ in open_heap)

        # Target-lock: stop opening new trades once we've effectively won.
        if equity >= params.target_usd * params.target_lock_pct:
            continue

        cap_for_strat = max(0.0, alloc * equity - deployed_by_strat[strat])

        # Adaptive sizing: more aggressive when far below target, less when close.
        size_pct = trade_pct_by_strat[strat]
        if params.adaptive_sizing:
            low_threshold = params.adaptive_low_mult * params.seed_usd
            high_threshold = params.adaptive_high_mult * params.target_usd
            if equity <= low_threshold:
                size_pct *= params.adaptive_low_boost
            elif equity >= high_threshold:
                size_pct *= params.adaptive_high_cut

        per_trade = min(
            bankroll * size_pct,
            bankroll * params.max_trade_pct,
            cap_for_strat,
        )
        # Don't enter dust-sized trades.
        if per_trade < 0.10:
            continue

        if strat == "S1":
            mult, duration_min = sample_s1_outcome(rng, params.outcomes.s1)
        elif strat == "S2":
            mult, duration_min = sample_s2_outcome(rng, params.outcomes.s2)
        else:
            mult, duration_min = sample_s3_outcome(rng, params.outcomes.s3)

        bankroll -= per_trade
        deployed_by_strat[strat] += per_trade
        close_min = ev_time + duration_min
        if close_min > horizon_min:
            close_min = horizon_min
        heapq.heappush(open_heap, (close_min, strat, per_trade, mult))

    # Close remaining positions at horizon end.
    _close_due(horizon_min + 1)
    final_equity = bankroll + sum(loc * mult for _, _, loc, mult in open_heap)
    # If we exited via "break", positions were already closed normally; this is
    # just a defensive belt-and-braces.
    trajectory.append((horizon_min, final_equity))
    return final_equity, trajectory, trades_log


def run_backtest(params: BacktestParams) -> BacktestSummary:
    rng_master = np.random.default_rng(params.seed_rng)
    finals: list[float] = []
    sample_trajectories: list[list[tuple[int, float]]] = []
    for sim in range(params.n_simulations):
        rng = np.random.default_rng(int(rng_master.integers(0, 2**31 - 1)))
        final, traj, _trades = _run_one_sim(params, rng)
        finals.append(final)
        if sim < 25:
            sample_trajectories.append(traj)
    arr = np.array(finals, dtype=float)
    seed = params.seed_usd
    counts, bins = np.histogram(
        arr,
        bins=np.concatenate(([0], np.geomspace(max(seed * 0.01, 0.01), max(arr.max(), seed * 20), 25))),
    )
    return BacktestSummary(
        n_simulations=params.n_simulations,
        seed_usd=params.seed_usd,
        target_usd=params.target_usd,
        horizon_days=params.horizon_days,
        median_final=float(np.median(arr)),
        mean_final=float(arr.mean()),
        p5_final=float(np.quantile(arr, 0.05)),
        p25_final=float(np.quantile(arr, 0.25)),
        p75_final=float(np.quantile(arr, 0.75)),
        p95_final=float(np.quantile(arr, 0.95)),
        max_final=float(arr.max()),
        p_hit_target=float((arr >= params.target_usd).mean()),
        p_hit_2x=float((arr >= 2 * seed).mean()),
        p_hit_3x=float((arr >= 3 * seed).mean()),
        p_hit_5x=float((arr >= 5 * seed).mean()),
        p_near_zero=float((arr <= seed * 0.2).mean()),
        sample_trajectories=sample_trajectories,
        histogram_bins=bins.tolist(),
        histogram_counts=counts.tolist(),
        params_json=json.dumps({
            "seed_usd": params.seed_usd,
            "target_usd": params.target_usd,
            "horizon_days": params.horizon_days,
            "n_simulations": params.n_simulations,
            "alloc_s1": params.alloc_s1,
            "alloc_s2": params.alloc_s2,
            "alloc_s3": params.alloc_s3,
            "s1_trade_pct": params.s1_trade_pct,
            "s2_trade_pct": params.s2_trade_pct,
            "s3_trade_pct": params.s3_trade_pct,
            "max_trade_pct": params.max_trade_pct,
            "daily_loss_cutoff_pct": params.daily_loss_cutoff_pct,
            "outcomes": params_to_dict(params.outcomes),
        }),
    )


def summary_to_jsonable(summary: BacktestSummary) -> dict:
    return {
        "n_simulations": summary.n_simulations,
        "seed_usd": summary.seed_usd,
        "target_usd": summary.target_usd,
        "horizon_days": summary.horizon_days,
        "median_final": summary.median_final,
        "mean_final": summary.mean_final,
        "p5_final": summary.p5_final,
        "p25_final": summary.p25_final,
        "p75_final": summary.p75_final,
        "p95_final": summary.p95_final,
        "max_final": summary.max_final,
        "p_hit_target": summary.p_hit_target,
        "p_hit_2x": summary.p_hit_2x,
        "p_hit_3x": summary.p_hit_3x,
        "p_hit_5x": summary.p_hit_5x,
        "p_near_zero": summary.p_near_zero,
        "sample_trajectories": [
            [{"minute": m, "equity": e} for (m, e) in traj]
            for traj in summary.sample_trajectories
        ],
        "histogram_bins": summary.histogram_bins,
        "histogram_counts": summary.histogram_counts,
        "params_json": summary.params_json,
    }
