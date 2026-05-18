"""Tests for the live-execution path of the runner and the withdraw-onchain endpoint.

These tests stub out the wallet (no real RPC) and Jupiter client (no real swap)
so we can exercise the wiring without touching Solana.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app import jupiter as jupiter_mod
from app import runner as runner_mod
from app import wallet as wallet_mod
from app.api.routes import router
from app.config import settings
from app.models import BotState
from app.runner import Runner
from app.strategies.base import Position


@dataclass
class FakeSwap:
    txid: str
    input_amount: int
    output_amount: int
    price_impact_pct: float = 0.0
    confirmed: bool = True


class FakeJupiterClient:
    """Records the calls made by the runner so tests can assert against them."""

    instances: list["FakeJupiterClient"] = []

    def __init__(self, wallet):
        self.wallet = wallet
        self.buys: list[tuple[str, int]] = []
        self.sells: list[tuple[str, int]] = []
        self.buy_returns: dict[str, FakeSwap] = {}
        self.sell_returns: dict[str, FakeSwap] = {}
        FakeJupiterClient.instances.append(self)

    async def buy_usdc_to_token(self, mint: str, usdc_units: int, slippage_bps: int = 200):
        self.buys.append((mint, usdc_units))
        if mint in self.buy_returns:
            return self.buy_returns[mint]
        return FakeSwap(
            txid=f"buy-{mint[:6]}",
            input_amount=usdc_units,
            output_amount=usdc_units * 100,  # 100 tokens per USDC for the purposes of tests
            confirmed=True,
        )

    async def sell_token_to_usdc(self, mint: str, token_units: int, slippage_bps: int = 300):
        self.sells.append((mint, token_units))
        if mint in self.sell_returns:
            return self.sell_returns[mint]
        # Default: sell at 2x mark (token bought at 100/USDC sold at 50/USDC = 2x).
        return FakeSwap(
            txid=f"sell-{mint[:6]}",
            input_amount=token_units,
            output_amount=token_units // 50,
            confirmed=True,
        )

    async def aclose(self):
        return None


@pytest.fixture
def fake_jupiter(monkeypatch):
    FakeJupiterClient.instances = []
    monkeypatch.setattr(runner_mod, "JupiterClient", FakeJupiterClient)
    monkeypatch.setattr(jupiter_mod, "JupiterClient", FakeJupiterClient)
    return FakeJupiterClient


@pytest.fixture
def fake_wallet(monkeypatch):
    """Install a fake WALLET module-level singleton so _is_live narrows."""
    wallet = MagicMock()
    wallet.pubkey_str.return_value = "FakeWalletPubkey11111111111111111111111111"
    wallet.get_token_decimals = AsyncMock(return_value=6)
    wallet.get_spl_token_amount_raw = AsyncMock(return_value=10_000_000)  # 10 USDC
    wallet.get_sol_balance = AsyncMock(return_value=0.05)
    wallet.transfer_spl_to = AsyncMock(return_value=("usdc-tx-sig", True))
    wallet.transfer_sol_to = AsyncMock(return_value=("sol-tx-sig", True))
    monkeypatch.setattr(wallet_mod, "WALLET", wallet)
    return wallet


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(settings, "live_trading_armed", True)


def _make_runner_with_live_state() -> tuple[Runner, BotState]:
    r = Runner()
    state = BotState(id=1, mode="live", bankroll_usd=10.0, bankroll_start_usd=10.0,
                     bankroll_day_start_usd=10.0)
    return r, state


@pytest.mark.asyncio
async def test_execute_open_live_records_token_units(fake_jupiter, fake_wallet, armed):
    runner, _state = _make_runner_with_live_state()
    res = await runner._execute_open_live("MintAddr1111111111111111111111111111111111", 2.0)
    assert res is not None
    assert res["token_units"] == 2_000_000 * 100  # 100 tokens per USDC
    assert res["spent_usdc"] == 2.0
    assert res["txid"].startswith("buy-")


@pytest.mark.asyncio
async def test_execute_open_live_returns_none_on_failure(fake_jupiter, fake_wallet, armed):
    runner, _state = _make_runner_with_live_state()
    # Make Jupiter return unconfirmed.
    def factory(_wallet):
        c = FakeJupiterClient(_wallet)
        c.buy_returns["MintAddrFail11111111111111111111111111111"] = FakeSwap(
            txid="buy-fail", input_amount=2_000_000, output_amount=0, confirmed=False,
        )
        return c
    runner_mod.JupiterClient = factory  # type: ignore[assignment]
    try:
        res = await runner._execute_open_live("MintAddrFail11111111111111111111111111111", 2.0)
        assert res is None
    finally:
        runner_mod.JupiterClient = FakeJupiterClient


@pytest.mark.asyncio
async def test_execute_close_live_returns_proceeds(fake_jupiter, fake_wallet, armed):
    runner, _state = _make_runner_with_live_state()
    pos = Position(
        symbol="DOGE", address="MintAddr2222222222222222222222222222222222",
        entry_price=0.01, size_usd=1.0, opened_at_tick=0, strategy="S1",
        token_units=1_000_000, entry_txid="buy-doge",
    )
    proceeds, txid, confirmed = await runner._execute_close_live(pos)
    assert confirmed is True
    assert txid.startswith("sell-")
    # token_units // 50 = 20000 USDC units = $0.02, but the assertion just checks > 0.
    assert proceeds > 0


def test_withdraw_onchain_requires_armed(monkeypatch):
    """The endpoint refuses the call if LIVE_TRADING_ARMED is false."""
    from fastapi import FastAPI
    monkeypatch.setattr(settings, "live_trading_armed", False)

    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    r = client.post("/api/control/withdraw-onchain", json={
        "confirmation_token": "doesntmatter",
        "destination_address": "FakeDest11111111111111111111111111111111111",
        "acknowledge_text": "I AGREE TO WITHDRAW",
    })
    assert r.status_code == 403
    assert "armed" in r.json()["detail"].lower()


def test_withdraw_onchain_wrong_ack_text(monkeypatch, fake_wallet, armed):
    """Wrong acknowledge_text returns 400."""
    from fastapi import FastAPI
    runner_mod.RUNNER = MagicMock()
    runner_mod.RUNNER.go_live_token = "TOKEN123"

    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    r = client.post("/api/control/withdraw-onchain", json={
        "confirmation_token": "TOKEN123",
        "destination_address": "FakeDest11111111111111111111111111111111111",
        "acknowledge_text": "wrong text but long enough",
    })
    assert r.status_code == 400
    assert "I AGREE" in r.json()["detail"]


def test_withdraw_onchain_refuses_in_live_mode(monkeypatch, fake_wallet, armed):
    """Withdraw refuses while runner is still in live mode."""
    from contextlib import contextmanager

    from fastapi import FastAPI

    from app import db as db_mod

    runner_mod.RUNNER = MagicMock()
    runner_mod.RUNNER.go_live_token = "TOKEN123"

    # Make session_scope yield a session that returns a live-mode BotState.
    mock_session = MagicMock()
    mock_state = BotState(id=1, mode="live", bankroll_usd=10.0)
    mock_session.exec.return_value.first.return_value = mock_state

    @contextmanager
    def fake_scope():
        yield mock_session

    monkeypatch.setattr(db_mod, "session_scope", fake_scope)
    # routes.py imports session_scope at module load; patch the imported binding too.
    from app.api import routes as routes_mod
    monkeypatch.setattr(routes_mod, "session_scope", fake_scope)

    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    r = client.post("/api/control/withdraw-onchain", json={
        "confirmation_token": "TOKEN123",
        "destination_address": "FakeDest11111111111111111111111111111111111",
        "acknowledge_text": "I AGREE TO WITHDRAW",
    })
    assert r.status_code == 400
    assert "live mode" in r.json()["detail"].lower()


def test_position_token_units_default():
    """Paper-mode positions default token_units=0."""
    pos = Position(
        symbol="X", address="addr", entry_price=1.0, size_usd=1.0,
        opened_at_tick=0, strategy="S1",
    )
    assert pos.token_units == 0
    assert pos.entry_txid == ""
    assert pos.exit_txid == ""
