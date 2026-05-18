"""Frozen "best" strategy parameters discovered by the Monte Carlo sweep.

Picked the config that maximized P(>=$100) within an acceptable P(<=$2) band.
Numbers below are produced by the backtester under the simulator's honest priors
(fees, slippage, missed entries, regime variability) and represent the
*honest* expected behavior of the system, not optimistic theory.

To re-tune: run /backtest/run with custom params from the dashboard.
"""
from __future__ import annotations

from dataclasses import asdict
import json

from .backtester import BacktestParams


def best_known_params() -> BacktestParams:
    return BacktestParams(
        seed_usd=10.0,
        target_usd=100.0,
        horizon_days=7,
        n_simulations=2000,
        alloc_s1=0.30,
        alloc_s2=0.70,
        alloc_s3=0.00,
        s1_trade_pct=0.20,
        s2_trade_pct=0.40,
        s3_trade_pct=0.00,
        adaptive_sizing=True,
        adaptive_low_boost=1.5,
        adaptive_high_cut=0.4,
        target_lock_pct=0.95,
        max_trade_pct=0.40,
        daily_loss_cutoff_pct=0.40,
    )


# What this config achieved in the sweep, recorded here as a "honesty receipt".
BACKTEST_HEADLINE_STATS = {
    "n_simulations": 2000,
    "p_hit_target": 0.144,   # 14.4%
    "p_hit_3x": 0.194,       # 19.4%
    "p_near_zero": 0.540,    # 54.0% chance of <= $2
    "median_final_usd": 1.58,
    "mean_final_usd": 32.95,
    "p95_final_usd": 175.11,
    "notes": [
        "Hit-target rate is ~1 in 7 under honest priors. NOT a guaranteed outcome.",
        "Half of simulations end below $2: this is a high-variance lottery, not investing.",
        "Mean > median by ~20x: outcome is dominated by rare big winners.",
        "Real live results will differ. Real edge erodes over time as more bots compete.",
    ],
}


def headline_stats_json() -> str:
    return json.dumps(BACKTEST_HEADLINE_STATS, indent=2)


def params_summary_dict() -> dict:
    p = best_known_params()
    return {
        "seed_usd": p.seed_usd,
        "target_usd": p.target_usd,
        "horizon_days": p.horizon_days,
        "alloc_s1": p.alloc_s1,
        "alloc_s2": p.alloc_s2,
        "alloc_s3": p.alloc_s3,
        "s1_trade_pct": p.s1_trade_pct,
        "s2_trade_pct": p.s2_trade_pct,
        "s3_trade_pct": p.s3_trade_pct,
        "adaptive_sizing": p.adaptive_sizing,
        "target_lock_pct": p.target_lock_pct,
        "max_trade_pct": p.max_trade_pct,
        "daily_loss_cutoff_pct": p.daily_loss_cutoff_pct,
        "headline_stats": BACKTEST_HEADLINE_STATS,
    }
