"""Pydantic schemas for API request/response bodies."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class StateOut(BaseModel):
    mode: str
    bankroll_usd: float
    bankroll_start_usd: float
    bankroll_day_start_usd: float
    deposits_usd: float
    withdrawals_usd: float
    halted_reason: str
    session_start_at: datetime
    last_tick_at: Optional[datetime]
    equity_usd: float
    open_positions: list[dict]
    target_usd: float
    horizon_days: int


class DepositIn(BaseModel):
    amount_usd: float = Field(gt=0, le=10000)


class WithdrawIn(BaseModel):
    amount_usd: float = Field(gt=0, le=10000)


class TradeOut(BaseModel):
    id: int
    opened_at: datetime
    closed_at: Optional[datetime]
    strategy: str
    symbol: str
    side: str
    size_usd: float
    entry_price: float
    exit_price: Optional[float]
    pnl_usd: Optional[float]
    pnl_pct: Optional[float]
    exit_reason: Optional[str]
    mode: str


class EquityPoint(BaseModel):
    at: datetime
    equity_usd: float
    mode: str


class BacktestIn(BaseModel):
    seed_usd: float = 10.0
    target_usd: float = 100.0
    horizon_days: int = 7
    n_simulations: int = 1500
    alloc_s1: float = 0.30
    alloc_s2: float = 0.70
    alloc_s3: float = 0.0
    s1_trade_pct: float = 0.20
    s2_trade_pct: float = 0.40
    s3_trade_pct: float = 0.0
    adaptive_sizing: bool = True
    max_trade_pct: float = 0.40
    daily_loss_cutoff_pct: float = 0.40
    target_lock_pct: float = 0.95


class BacktestRunSummaryOut(BaseModel):
    id: int
    created_at: datetime
    seed_usd: float
    target_usd: float
    horizon_days: int
    n_simulations: int
    p_hit_target: float
    p_near_zero: float
    median_final: float
    mean_final: float
    p5_final: float
    p25_final: float
    p75_final: float
    p95_final: float
    max_final: float


class GoLiveIn(BaseModel):
    confirmation_token: str
    acknowledge_risk_text: str = Field(
        ..., min_length=20,
        description='Type the disclosure: "I understand this is a high-variance speculative bot with ~14% historical P(hit $100) and ~54% P(near-zero) under honest priors."'
    )


class WalletStatusOut(BaseModel):
    address: str
    sol_balance: float
    usdc_balance: float
    funded: bool  # True once usdc_balance >= 1.0 and sol_balance >= 0.01
    armed_env: bool
    rpc_url: str


class TestSwapIn(BaseModel):
    confirmation_token: str
    usdc_amount: float = Field(default=1.0, gt=0, le=5.0,
                               description="Notional $ to round-trip USDC->SOL->USDC. Capped at $5 to limit blast radius.")


class TestSwapOut(BaseModel):
    ok: bool
    buy_txid: Optional[str] = None
    buy_confirmed: bool = False
    sell_txid: Optional[str] = None
    sell_confirmed: bool = False
    input_usdc: float
    output_usdc: Optional[float] = None
    round_trip_cost_pct: Optional[float] = None
    error: Optional[str] = None
