"""Outcome distributions for the Monte Carlo backtester.

These distributions are the single biggest source of model risk in the whole
system. They are MY priors based on what is publicly known about Solana
memecoin launches (pump.fun graduation rate, typical TP/SL hit rates, fat-
tail moonshot frequency). They are NOT calibrated to a private dataset, and
they ARE intentionally on the pessimistic side -- the goal is to under-
promise on the backtest so live results don't disappoint.

Every distribution is exposed as tunable parameters so we can stress-test how
sensitive the final return distribution is to these assumptions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np


# ----- S1 momentum sniper outcomes (per filtered entry) ------------------------
# Tighter, more pessimistic than initial calibration. Includes:
#  - higher rug + stop frequency
#  - lower moonshot rate (~0.8%)
#  - round-trip cost (DEX swap fees + slippage) modeled separately below
@dataclass
class S1Params:
    p_full_rug: float = 0.34        # exit -75% to -95% incl. SL slippage
    p_stop: float = 0.33            # exit at SL -40%
    p_timeout_loss: float = 0.16    # exit at mild loss (-20%..0)
    p_take_profit: float = 0.12     # exit at 2x TP
    p_trail_win: float = 0.04       # exit via trailing stop, 3x-6x
    p_moon: float = 0.01            # exit via trailing stop, 8x-25x

    rug_low: float = 0.05; rug_high: float = 0.25
    stop_mid: float = 0.60
    timeout_low: float = 0.80; timeout_high: float = 1.05
    tp_mid: float = 2.00
    trail_low: float = 3.0; trail_high: float = 6.0
    moon_low: float = 8.0; moon_high: float = 25.0

    # Per-round-trip execution drag (entry + exit combined).
    # Pump.fun / Raydium pool fees + 1-3% slippage on tiny illiquid books.
    round_trip_drag: float = 0.035  # 3.5% lost to costs on every trade
    # Probability we *fail to enter* (front-run by faster bots).
    p_miss_entry: float = 0.35

    close_min_min: int = 5
    close_max_min: int = 360


# ----- S2 migration sniper outcomes -------------------------------------------
@dataclass
class S2Params:
    p_full_rug: float = 0.28
    p_stop: float = 0.36
    p_timeout_loss: float = 0.16
    p_take_profit: float = 0.11
    p_trail_win: float = 0.06
    p_moon: float = 0.03

    rug_low: float = 0.05; rug_high: float = 0.30
    stop_mid: float = 0.50
    timeout_low: float = 0.70; timeout_high: float = 1.10
    tp_mid: float = 3.00
    trail_low: float = 4.0; trail_high: float = 9.0
    moon_low: float = 12.0; moon_high: float = 50.0

    round_trip_drag: float = 0.045  # bigger drag - fresh LPs slip more
    p_miss_entry: float = 0.45

    close_min_min: int = 10
    close_max_min: int = 720


# ----- S3 scalp outcomes ------------------------------------------------------
@dataclass
class S3Params:
    win_rate: float = 0.56
    target_profit_pct: float = 0.008
    stop_loss_pct: float = 0.018
    timeout_drift_low: float = -0.004
    timeout_drift_high: float = 0.002
    p_timeout: float = 0.18

    # Solana DEX swap fee ~0.05% per side + a few bps slippage on SOL/USDC.
    round_trip_drag: float = 0.0025  # 0.25% per round trip
    p_miss_entry: float = 0.05

    close_min_min: int = 5
    close_max_min: int = 60


# ----- Arrival rates (opportunities per day that pass each strategy's filter) -
# Conservative-ish: real pump.fun launch volume is huge, but very few pass a
# strict early-momentum filter (LP locked, dev wallet not dumping, real volume).
@dataclass
class ArrivalParams:
    s1_per_day: float = 18.0  # tightened from 28
    s2_per_day: float = 5.0   # tightened from 7
    s3_per_day: float = 80.0  # tightened from 110
    daily_regime_sigma: float = 0.45  # bigger - markets are more variable


@dataclass
class StrategyOutcomeParams:
    s1: S1Params = field(default_factory=S1Params)
    s2: S2Params = field(default_factory=S2Params)
    s3: S3Params = field(default_factory=S3Params)
    arrivals: ArrivalParams = field(default_factory=ArrivalParams)


def _sample_uniform(rng: np.random.Generator, lo: float, hi: float) -> float:
    return float(rng.uniform(lo, hi))


MISSED_OUTCOME = (1.0, 1)  # if we fail to enter, capital returns intact one tick later


def sample_s1_outcome(rng: np.random.Generator, p: S1Params) -> tuple[float, int]:
    if rng.random() < p.p_miss_entry:
        return MISSED_OUTCOME
    weights = np.array(
        [p.p_full_rug, p.p_stop, p.p_timeout_loss, p.p_take_profit, p.p_trail_win, p.p_moon],
        dtype=float,
    )
    weights = weights / weights.sum()
    mode = int(rng.choice(6, p=weights))
    if mode == 0:
        mult = _sample_uniform(rng, p.rug_low, p.rug_high)
    elif mode == 1:
        mult = p.stop_mid + float(rng.normal(0, 0.02))
    elif mode == 2:
        mult = _sample_uniform(rng, p.timeout_low, p.timeout_high)
    elif mode == 3:
        mult = p.tp_mid + float(rng.normal(0, 0.05))
    elif mode == 4:
        mult = _sample_uniform(rng, p.trail_low, p.trail_high)
    else:
        # Log-uniform across the moon range to weight rarer huge wins less.
        mult = float(np.exp(rng.uniform(np.log(p.moon_low), np.log(p.moon_high))))
    mult *= (1.0 - p.round_trip_drag)  # fees + slippage
    duration = int(rng.integers(p.close_min_min, p.close_max_min + 1))
    return max(mult, 0.0), duration


def sample_s2_outcome(rng: np.random.Generator, p: S2Params) -> tuple[float, int]:
    if rng.random() < p.p_miss_entry:
        return MISSED_OUTCOME
    weights = np.array(
        [p.p_full_rug, p.p_stop, p.p_timeout_loss, p.p_take_profit, p.p_trail_win, p.p_moon],
        dtype=float,
    )
    weights = weights / weights.sum()
    mode = int(rng.choice(6, p=weights))
    if mode == 0:
        mult = _sample_uniform(rng, p.rug_low, p.rug_high)
    elif mode == 1:
        mult = p.stop_mid + float(rng.normal(0, 0.02))
    elif mode == 2:
        mult = _sample_uniform(rng, p.timeout_low, p.timeout_high)
    elif mode == 3:
        mult = p.tp_mid + float(rng.normal(0, 0.08))
    elif mode == 4:
        mult = _sample_uniform(rng, p.trail_low, p.trail_high)
    else:
        mult = float(np.exp(rng.uniform(np.log(p.moon_low), np.log(p.moon_high))))
    mult *= (1.0 - p.round_trip_drag)
    duration = int(rng.integers(p.close_min_min, p.close_max_min + 1))
    return max(mult, 0.0), duration


def sample_s3_outcome(rng: np.random.Generator, p: S3Params) -> tuple[float, int]:
    if rng.random() < p.p_miss_entry:
        return MISSED_OUTCOME
    # Outcome: TP / SL / timeout
    r = rng.random()
    if r < p.p_timeout:
        mult = 1.0 + _sample_uniform(rng, p.timeout_drift_low, p.timeout_drift_high)
    elif r < p.p_timeout + (1 - p.p_timeout) * p.win_rate:
        mult = 1.0 + p.target_profit_pct + float(rng.normal(0, 0.001))
    else:
        mult = 1.0 - p.stop_loss_pct + float(rng.normal(0, 0.002))
    mult *= (1.0 - p.round_trip_drag)
    duration = int(rng.integers(p.close_min_min, p.close_max_min + 1))
    return max(mult, 0.0), duration


def sanity_check_distributions(rng: np.random.Generator | None = None) -> dict:
    """Quick reality check on the distributions; useful for tests."""
    rng = rng or np.random.default_rng(0)
    p = StrategyOutcomeParams()
    s1 = np.array([sample_s1_outcome(rng, p.s1)[0] for _ in range(20_000)])
    s2 = np.array([sample_s2_outcome(rng, p.s2)[0] for _ in range(20_000)])
    s3 = np.array([sample_s3_outcome(rng, p.s3)[0] for _ in range(20_000)])
    return {
        "s1": {"mean": float(s1.mean()), "median": float(np.median(s1)), "p95": float(np.quantile(s1, 0.95))},
        "s2": {"mean": float(s2.mean()), "median": float(np.median(s2)), "p95": float(np.quantile(s2, 0.95))},
        "s3": {"mean": float(s3.mean()), "median": float(np.median(s3)), "p95": float(np.quantile(s3, 0.95))},
    }


def params_to_dict(p: StrategyOutcomeParams) -> dict:
    return {"s1": asdict(p.s1), "s2": asdict(p.s2), "s3": asdict(p.s3), "arrivals": asdict(p.arrivals)}
