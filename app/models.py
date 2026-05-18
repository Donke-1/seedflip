"""Database models. Kept intentionally small."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BotState(SQLModel, table=True):
    """Single-row table holding global bot state."""

    id: int = Field(default=1, primary_key=True)
    mode: str = Field(default="paper")  # "paper" | "live" | "halted"
    bankroll_usd: float = Field(default=10.0)
    bankroll_start_usd: float = Field(default=10.0)
    bankroll_day_start_usd: float = Field(default=10.0)
    day_start_at: datetime = Field(default_factory=_utcnow)
    session_start_at: datetime = Field(default_factory=_utcnow)
    halted_reason: str = Field(default="")
    last_tick_at: Optional[datetime] = Field(default=None)
    deposits_usd: float = Field(default=0.0)
    withdrawals_usd: float = Field(default=0.0)


class Trade(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    opened_at: datetime = Field(default_factory=_utcnow, index=True)
    closed_at: Optional[datetime] = Field(default=None, index=True)
    strategy: str = Field(index=True)  # "S1" | "S2" | "S3"
    symbol: str
    side: str = Field(default="long")
    size_usd: float
    entry_price: float
    exit_price: Optional[float] = Field(default=None)
    pnl_usd: Optional[float] = Field(default=None)
    pnl_pct: Optional[float] = Field(default=None)
    exit_reason: Optional[str] = Field(default=None)  # "tp" | "sl" | "timeout" | "manual"
    mode: str = Field(default="paper")  # "paper" | "live" | "backtest"
    run_id: Optional[str] = Field(default=None, index=True)


class EquitySnapshot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=_utcnow, index=True)
    equity_usd: float
    mode: str = Field(default="paper")


class BacktestRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    seed_usd: float
    target_usd: float
    horizon_days: int
    n_simulations: int
    params_json: str = ""
    # Summary stats
    p_hit_target: float = 0.0
    p_near_zero: float = 0.0
    median_final: float = 0.0
    mean_final: float = 0.0
    p5_final: float = 0.0
    p25_final: float = 0.0
    p75_final: float = 0.0
    p95_final: float = 0.0
    max_final: float = 0.0
    distribution_json: str = ""  # JSON-encoded list of final equities
