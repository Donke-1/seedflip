"""Runtime configuration for SeedFlip.

Live trading is hard-gated behind two explicit flags so that a misconfiguration
cannot accidentally move real funds. Even with both flags set we still default
to paper trading and require an explicit /control/go-live POST.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    seed_capital_usd: float = 10.0
    target_capital_usd: float = 100.0
    horizon_days: int = 7

    # Tiered allocation of the seed at session start.
    # S3 anchor (mean-reversion) gets the biggest slice for capital preservation,
    # S1 momentum is the workhorse, S2 migration is the lottery tier.
    alloc_s1_momentum: float = 0.20
    alloc_s2_migration: float = 0.10
    alloc_s3_scalp: float = 0.70

    # Risk knobs (per-trade).
    max_trade_pct_of_bankroll: float = 0.20
    daily_loss_cutoff_pct: float = 0.35  # if bankroll dips this far below day-start, halt.
    dust_floor_usd: float = 0.50  # below this, stop trading entirely.

    # Strategy knobs (tunable via /control/tune).
    s1_take_profit_mult: float = 2.0
    s1_stop_loss_pct: float = 0.40
    s1_min_holders: int = 25
    s1_max_age_minutes: int = 10

    s2_take_profit_mult: float = 3.0
    s2_stop_loss_pct: float = 0.50

    s3_grid_band_pct: float = 0.015  # +/- 1.5% mean-reversion band
    s3_target_profit_pct: float = 0.008  # 0.8% per scalp
    s3_max_concurrent: int = 3

    # Deployment / persistence
    db_path: str = ""

    # Live trading gates
    live_trading_armed: bool = False  # global hard gate (env)
    # When set, the runner refuses any test-swap or live trade unless the request
    # body carries this confirmation token. Generated once at boot, logged once.
    # (Operator copies the value from the boot log into the dashboard.)

    # External APIs (all optional, public endpoints by default)
    dexscreener_base_url: str = "https://api.dexscreener.com"
    geckoterminal_base_url: str = "https://api.geckoterminal.com/api/v2"

    # Solana RPC. Default to the public mainnet-beta endpoint; this is heavily
    # rate-limited. Override via env (e.g. Helius / QuickNode) for production.
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    # Optional override path for the wallet secret (test fixture friendly).
    wallet_secret_path: str = ""

    def resolved_db_path(self) -> str:
        if self.db_path:
            return self.db_path
        # /data exists when deployed with volume=True
        if Path("/data").is_dir() and os.access("/data", os.W_OK):
            return "/data/seedflip.db"
        return str(Path(__file__).resolve().parent.parent / "data" / "seedflip.db")


settings = Settings()
