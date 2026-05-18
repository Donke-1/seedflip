"""HTTP endpoints."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, desc

from .. import runner as runner_mod
from .. import wallet as wallet_mod
from ..backtester import BacktestParams, run_backtest, summary_to_jsonable
from ..config import settings
from ..db import get_session
from ..jupiter import JupiterClient
from ..models import BacktestRun, BotState, EquitySnapshot, Trade
from ..strategy_config import params_summary_dict, BACKTEST_HEADLINE_STATS
from .schemas import (
    BacktestIn,
    BacktestRunSummaryOut,
    DepositIn,
    EquityPoint,
    GoLiveIn,
    StateOut,
    TestSwapIn,
    TestSwapOut,
    TradeOut,
    WalletStatusOut,
    WithdrawIn,
)

log = logging.getLogger(__name__)

router = APIRouter()


def _state_or_init(session: Session) -> BotState:
    state = session.exec(select(BotState)).first()
    if state is None:
        state = BotState(
            id=1,
            mode="paper",
            bankroll_usd=settings.seed_capital_usd,
            bankroll_start_usd=settings.seed_capital_usd,
            bankroll_day_start_usd=settings.seed_capital_usd,
        )
        session.add(state)
        session.commit()
        session.refresh(state)
    return state


# ----- state ------------------------------------------------------------------
@router.get("/state", response_model=StateOut)
def get_state(session: Session = Depends(get_session)) -> StateOut:
    state = _state_or_init(session)
    open_positions = runner_mod.RUNNER.open_positions_snapshot() if runner_mod.RUNNER else []
    equity = state.bankroll_usd + sum(p["size_usd"] for p in open_positions)
    return StateOut(
        mode=state.mode,
        bankroll_usd=state.bankroll_usd,
        bankroll_start_usd=state.bankroll_start_usd,
        bankroll_day_start_usd=state.bankroll_day_start_usd,
        deposits_usd=state.deposits_usd,
        withdrawals_usd=state.withdrawals_usd,
        halted_reason=state.halted_reason,
        session_start_at=state.session_start_at,
        last_tick_at=state.last_tick_at,
        equity_usd=equity,
        open_positions=open_positions,
        target_usd=settings.target_capital_usd,
        horizon_days=settings.horizon_days,
    )


@router.get("/config")
def get_config() -> dict:
    return {
        "live_trading_armed_env": settings.live_trading_armed,
        "strategy_params": params_summary_dict(),
        "headline_stats": BACKTEST_HEADLINE_STATS,
        "go_live_token_hint": "Token is logged to backend stderr on startup; copy it from /control/go-live-token after authenticating in production.",
    }


# ----- trades / equity --------------------------------------------------------
@router.get("/trades")
def list_trades(
    limit: int = 200,
    session: Session = Depends(get_session),
) -> list[dict]:
    limit = max(1, min(1000, limit))
    stmt = select(Trade).order_by(desc(Trade.id)).limit(limit)
    rows = session.exec(stmt).all()
    return [
        {
            "id": t.id,
            "opened_at": t.opened_at,
            "closed_at": t.closed_at,
            "strategy": t.strategy,
            "symbol": t.symbol,
            "side": t.side,
            "size_usd": t.size_usd,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "pnl_usd": t.pnl_usd,
            "pnl_pct": t.pnl_pct,
            "exit_reason": t.exit_reason,
            "mode": t.mode,
        }
        for t in rows
    ]


@router.get("/equity")
def list_equity(limit: int = 500, session: Session = Depends(get_session)) -> list[dict]:
    limit = max(1, min(5000, limit))
    stmt = select(EquitySnapshot).order_by(desc(EquitySnapshot.at)).limit(limit)
    rows = list(reversed(session.exec(stmt).all()))
    return [{"at": r.at, "equity_usd": r.equity_usd, "mode": r.mode} for r in rows]


@router.get("/attribution")
def pnl_attribution(session: Session = Depends(get_session)) -> dict:
    """Aggregate realized PnL by strategy."""
    stmt = select(Trade).where(Trade.pnl_usd != None)  # noqa: E711
    rows = session.exec(stmt).all()
    by_strat: dict[str, dict] = {}
    for r in rows:
        agg = by_strat.setdefault(r.strategy, {"n_trades": 0, "realized_usd": 0.0, "wins": 0, "losses": 0})
        agg["n_trades"] += 1
        agg["realized_usd"] += float(r.pnl_usd or 0.0)
        if (r.pnl_usd or 0.0) >= 0:
            agg["wins"] += 1
        else:
            agg["losses"] += 1
    return by_strat


# ----- control ----------------------------------------------------------------
@router.post("/control/deposit")
def deposit(payload: DepositIn, session: Session = Depends(get_session)) -> dict:
    state = _state_or_init(session)
    state.bankroll_usd += payload.amount_usd
    state.deposits_usd += payload.amount_usd
    session.add(state)
    return {"ok": True, "bankroll_usd": state.bankroll_usd}


@router.post("/control/withdraw")
def withdraw(payload: WithdrawIn, session: Session = Depends(get_session)) -> dict:
    state = _state_or_init(session)
    if payload.amount_usd > state.bankroll_usd:
        raise HTTPException(status_code=400, detail="Insufficient free bankroll. Close open positions first.")
    state.bankroll_usd -= payload.amount_usd
    state.withdrawals_usd += payload.amount_usd
    session.add(state)
    return {"ok": True, "bankroll_usd": state.bankroll_usd}


@router.post("/control/halt")
def halt(session: Session = Depends(get_session)) -> dict:
    state = _state_or_init(session)
    state.mode = "halted"
    state.halted_reason = "manual"
    session.add(state)
    return {"ok": True, "mode": state.mode}


@router.post("/control/resume")
def resume(session: Session = Depends(get_session)) -> dict:
    state = _state_or_init(session)
    state.mode = "paper"
    state.halted_reason = ""
    session.add(state)
    return {"ok": True, "mode": state.mode}


@router.post("/control/reset")
def reset(session: Session = Depends(get_session)) -> dict:
    """Reset bankroll to seed, clear trades + equity history. Useful for testing."""
    state = _state_or_init(session)
    state.bankroll_usd = settings.seed_capital_usd
    state.bankroll_start_usd = settings.seed_capital_usd
    state.bankroll_day_start_usd = settings.seed_capital_usd
    state.deposits_usd = 0.0
    state.withdrawals_usd = 0.0
    state.mode = "paper"
    state.halted_reason = ""
    state.session_start_at = datetime.now(timezone.utc)
    state.day_start_at = datetime.now(timezone.utc)
    state.last_tick_at = None
    session.add(state)
    for t in session.exec(select(Trade)).all():
        session.delete(t)
    for e in session.exec(select(EquitySnapshot)).all():
        session.delete(e)
    return {"ok": True, "bankroll_usd": state.bankroll_usd}


@router.get("/control/go-live-token")
def go_live_token() -> dict:
    """Returns the one-time token required to flip from paper to live mode.

    In production the dashboard never displays this; you copy it from the
    backend logs or stash it as a secret.
    """
    if not runner_mod.RUNNER:
        raise HTTPException(status_code=503, detail="Runner not started")
    return {"token": runner_mod.RUNNER.go_live_token, "live_armed_env": settings.live_trading_armed}


@router.post("/control/go-live")
def go_live(payload: GoLiveIn, session: Session = Depends(get_session)) -> dict:
    if not settings.live_trading_armed:
        raise HTTPException(status_code=403, detail="Live trading not armed (env LIVE_TRADING_ARMED must be true)")
    if not runner_mod.RUNNER:
        raise HTTPException(status_code=503, detail="Runner not started")
    if payload.confirmation_token != runner_mod.RUNNER.go_live_token:
        raise HTTPException(status_code=403, detail="Bad confirmation token")
    state = _state_or_init(session)
    state.mode = "live"
    state.halted_reason = ""
    session.add(state)
    return {"ok": True, "mode": state.mode, "warning": "Live mode set. The runner will now submit on-chain Jupiter swaps."}


# ----- wallet + test-swap -----------------------------------------------------
@router.get("/wallet", response_model=WalletStatusOut)
async def get_wallet() -> WalletStatusOut:
    if wallet_mod.WALLET is None:
        raise HTTPException(status_code=503, detail="Wallet not initialized")
    try:
        balances = await wallet_mod.WALLET.get_balances()
    except Exception as e:  # noqa: BLE001
        # RPC may be rate-limited; still surface the address.
        return WalletStatusOut(
            address=wallet_mod.WALLET.pubkey_str(),
            sol_balance=-1.0,
            usdc_balance=-1.0,
            funded=False,
            armed_env=settings.live_trading_armed,
            rpc_url=settings.solana_rpc_url,
        )
    return WalletStatusOut(
        address=balances["address"],
        sol_balance=balances["sol"],
        usdc_balance=balances["usdc"],
        funded=(balances["sol"] >= 0.01 and balances["usdc"] >= 1.0),
        armed_env=settings.live_trading_armed,
        rpc_url=settings.solana_rpc_url,
    )


@router.post("/control/test-swap", response_model=TestSwapOut)
async def control_test_swap(payload: TestSwapIn) -> TestSwapOut:
    """Forced USDC -> SOL -> USDC round-trip. Validates keypair + signing + RPC + Jupiter route.

    This is the gate that must succeed before autonomous live mode is allowed.
    Capped at $5 to limit blast radius. Requires the same one-time confirmation
    token as /control/go-live.
    """
    if not settings.live_trading_armed:
        raise HTTPException(status_code=403, detail="Live trading not armed (env LIVE_TRADING_ARMED must be true)")
    if not runner_mod.RUNNER:
        raise HTTPException(status_code=503, detail="Runner not started")
    if payload.confirmation_token != runner_mod.RUNNER.go_live_token:
        raise HTTPException(status_code=403, detail="Bad confirmation token")
    if wallet_mod.WALLET is None:
        raise HTTPException(status_code=503, detail="Wallet not initialized")

    usdc_units = int(round(payload.usdc_amount * 1_000_000))
    jup = JupiterClient(wallet_mod.WALLET)
    try:
        buy, sell = await jup.roundtrip_test_usdc(usdc_units)
    except Exception as e:  # noqa: BLE001
        log.exception("test-swap failed")
        return TestSwapOut(ok=False, input_usdc=payload.usdc_amount, error=str(e))
    finally:
        await jup.aclose()

    output_usdc = sell.output_amount / 1_000_000 if sell.confirmed else None
    cost_pct = None
    if output_usdc is not None and payload.usdc_amount > 0:
        cost_pct = 1.0 - (output_usdc / payload.usdc_amount)
    return TestSwapOut(
        ok=(buy.confirmed and sell.confirmed),
        buy_txid=buy.txid or None,
        buy_confirmed=buy.confirmed,
        sell_txid=sell.txid or None,
        sell_confirmed=sell.confirmed,
        input_usdc=payload.usdc_amount,
        output_usdc=output_usdc,
        round_trip_cost_pct=cost_pct,
    )


# ----- backtest ---------------------------------------------------------------
@router.post("/backtest/run")
def backtest_run(payload: BacktestIn, session: Session = Depends(get_session)) -> dict:
    params = BacktestParams(
        seed_usd=payload.seed_usd,
        target_usd=payload.target_usd,
        horizon_days=payload.horizon_days,
        n_simulations=max(100, min(5000, payload.n_simulations)),
        alloc_s1=payload.alloc_s1,
        alloc_s2=payload.alloc_s2,
        alloc_s3=payload.alloc_s3,
        s1_trade_pct=payload.s1_trade_pct,
        s2_trade_pct=payload.s2_trade_pct,
        s3_trade_pct=payload.s3_trade_pct,
        adaptive_sizing=payload.adaptive_sizing,
        max_trade_pct=payload.max_trade_pct,
        daily_loss_cutoff_pct=payload.daily_loss_cutoff_pct,
        target_lock_pct=payload.target_lock_pct,
    )
    summary = run_backtest(params)
    payload_j = summary_to_jsonable(summary)
    # Persist the run row.
    row = BacktestRun(
        seed_usd=params.seed_usd,
        target_usd=params.target_usd,
        horizon_days=params.horizon_days,
        n_simulations=params.n_simulations,
        params_json=summary.params_json,
        p_hit_target=summary.p_hit_target,
        p_near_zero=summary.p_near_zero,
        median_final=summary.median_final,
        mean_final=summary.mean_final,
        p5_final=summary.p5_final,
        p25_final=summary.p25_final,
        p75_final=summary.p75_final,
        p95_final=summary.p95_final,
        max_final=summary.max_final,
        distribution_json=json.dumps({"bins": summary.histogram_bins, "counts": summary.histogram_counts}),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return {"id": row.id, **payload_j}


@router.get("/backtest/runs", response_model=list[BacktestRunSummaryOut])
def backtest_list(limit: int = 50, session: Session = Depends(get_session)) -> list[dict]:
    limit = max(1, min(200, limit))
    rows = session.exec(select(BacktestRun).order_by(desc(BacktestRun.id)).limit(limit)).all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "seed_usd": r.seed_usd,
            "target_usd": r.target_usd,
            "horizon_days": r.horizon_days,
            "n_simulations": r.n_simulations,
            "p_hit_target": r.p_hit_target,
            "p_near_zero": r.p_near_zero,
            "median_final": r.median_final,
            "mean_final": r.mean_final,
            "p5_final": r.p5_final,
            "p25_final": r.p25_final,
            "p75_final": r.p75_final,
            "p95_final": r.p95_final,
            "max_final": r.max_final,
        }
        for r in rows
    ]


@router.get("/backtest/runs/{run_id}")
def backtest_detail(run_id: int, session: Session = Depends(get_session)) -> dict:
    row = session.get(BacktestRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return {
        "id": row.id,
        "created_at": row.created_at,
        "seed_usd": row.seed_usd,
        "target_usd": row.target_usd,
        "horizon_days": row.horizon_days,
        "n_simulations": row.n_simulations,
        "p_hit_target": row.p_hit_target,
        "p_near_zero": row.p_near_zero,
        "median_final": row.median_final,
        "mean_final": row.mean_final,
        "p5_final": row.p5_final,
        "p25_final": row.p25_final,
        "p75_final": row.p75_final,
        "p95_final": row.p95_final,
        "max_final": row.max_final,
        "params_json": row.params_json,
        "distribution_json": row.distribution_json,
    }
