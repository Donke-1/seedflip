# SeedFlip

A small autonomous bot that tries to turn **$10 USDC** into a target balance on
Solana by trading meme tokens through [Jupiter](https://jup.ag) over a fixed
horizon (default: **7 days**).

> [!WARNING]
> **This is gambling, not investing.**
>
> Per the bot's own honest backtest (n=2000 sims under documented priors), the
> single most likely outcome is **walking away with about $1–$2**. About **1
> in 7** runs hit the $100 target. Read [Risk reality](#risk-reality) before
> you deposit a cent.

---

## What it does

1. You deposit **USDC** (not USDT — see [Stablecoin](#stablecoin)) into the
   bot's Solana wallet.
2. The bot runs three strategies in parallel:
   - **S1 — Momentum:** breakout entries with trailing stops.
   - **S2 — Migration:** rides post-bonding-curve graduations.
   - **S3 — Scalp:** small, high-frequency moves.
3. It uses Jupiter to actually buy and sell SPL tokens on-chain. Position
   sizes are kelly-fractioned against the live bankroll.
4. After your chosen horizon (default 7 days) you can withdraw all USDC and
   any leftover SOL to any Solana address.

Everything is fully self-contained — wallet keypair, SQLite DB, and runtime
state all live on a single 1 GB Fly.io volume.

## Risk reality

The honest priors baked into the bot give this distribution for a 7-day,
$10 deposit run:

| metric                       | value     |
|------------------------------|-----------|
| Median final bankroll        | **$1.58** |
| Mean final bankroll          | $32.95    |
| P95 final bankroll           | $175.11   |
| **P(hit $100 target)**       | **14.4%** |
| **P(end ≤ $2)**              | **54.0%** |

In other words: half of all runs will end with you holding ~the price of a
soda. One run in seven will hit the target. There is no edge here that
overcomes those base rates.

## Stablecoin

The bot trades **USDC** (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`),
NOT USDT (`Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB`). They are
different SPL mints. If you accidentally send USDT, the dashboard will
surface a warning and you'll need to swap it to USDC at [jup.ag](https://jup.ag)
or send a fresh USDC deposit.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Browser                                                  │
│  React dashboard (Vite + Tailwind + shadcn/ui)            │
└──────────────────────────────┬────────────────────────────┘
                               │ HTTPS
┌──────────────────────────────▼────────────────────────────┐
│  Fly.io VM (shared-cpu-1x, ams)                           │
│  ┌──────────────────────────────────────────────────┐    │
│  │  FastAPI app                                     │    │
│  │   - / (static dashboard)                         │    │
│  │   - /api/wallet, /api/state, /api/positions, ... │    │
│  │   - /api/control/{halt,resume,go-live,...}       │    │
│  │   - /api/control/test-swap                       │    │
│  │   - /api/control/withdraw-onchain                │    │
│  │  Runner thread (async loop)                      │    │
│  │   - data_sources → strategies → executor         │    │
│  │   - live mode → Jupiter swap RPC                 │    │
│  └─────────────────────┬────────────────────────────┘    │
│                        │ JSON-RPC                          │
│  ┌─────────────────────▼────────────────────────────┐    │
│  │  Volume: /data (1 GB, persistent)                │    │
│  │   - wallet.json   (ed25519 keypair, 0600)        │    │
│  │   - seedflip.db   (SQLite — state, trades, ...)  │    │
│  └──────────────────────────────────────────────────┘    │
└──────────────────────────────┬────────────────────────────┘
                               │ HTTPS
                ┌──────────────▼──────────────┐
                │  Solana RPC (Helius, public)│
                │  Jupiter swap API           │
                └─────────────────────────────┘
```

## End-to-end flow

### 0. One-time deploy (operator)

```bash
# Authenticate (token saved in env from Fly.io org).
fly auth token "$FLY_API_TOKEN"

# Launch with persistent volume (idempotent).
fly launch --no-deploy --name seedflip --region ams --org "FREDRICK KIIRU"
fly volumes create seedflip_data --region ams --size 1
fly secrets set \
    SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY \
    LIVE_TRADING_ARMED=false \
    BANKROLL_USD_INITIAL=10 \
    HORIZON_HOURS=168 \
    TARGET_USD=100
fly deploy
```

### 1. Open the dashboard

Visit `https://seedflip.fly.dev`. The wallet section shows a Solana address
that the app generated on first boot — this is **your** wallet, stored at
`/data/wallet.json` on the volume. Nobody else has the private key.

### 2. Deposit USDC

From any Solana wallet (Phantom, Solflare, …) send:

- **$10 worth of USDC** (USDC mint: `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`)
- **0.05 SOL** for transaction fees and ATA rent

Send these to the address shown in the wallet section. The dashboard
auto-refreshes every 30 s; "Funded" will turn green once both arrive.

### 3. Smoke test with a $1 round-trip

Before arming live trading, the dashboard runs a `/api/control/test-swap`
that swaps **$1 USDC → SOL → USDC** via Jupiter. The end-state should
return roughly $1 USDC (a few cents of slippage is normal). If this fails,
**do not arm**.

### 4. Arm live trading

The "Live trading gate" section requires:

- The runner already started (boot-time token visible in the gate).
- A successful test-swap on this boot.
- Ticking "I understand I can lose everything".
- Posting that to `/api/control/go-live` with the boot token.

Once armed, the runner's `_maybe_open_positions` and `_maybe_close_positions`
call Jupiter live for entries and exits — no more paper trades.

### 5. Wait

By default, the bot runs autonomously for **7 days** (`HORIZON_HOURS=168`).
It will halt itself on target hit (`bankroll >= $100`) or on circuit
breakers (max drawdown, daily loss cap, kill-switch).

### 6. Withdraw

When you're ready to exit:

1. POST `/api/control/halt` to stop opening new positions.
2. Wait for any open position to close (or POST `/api/control/halt` again
   with `close_open: true`).
3. From the "Withdraw" section of the dashboard, enter:
   - Destination Solana address (your Phantom / Solflare / exchange address).
   - "I AGREE TO WITHDRAW" exactly.
   - The boot confirmation token.
   - Toggle "include SOL dust" if you want leftover SOL too.
4. The endpoint sweeps **all USDC** and (optionally) SOL above the rent
   reserve into one or two on-chain transfers and returns the tx signatures.

## Local development

```bash
# Backend.
poetry install
poetry run uvicorn app.main:app --reload

# Frontend (separate terminal).
cd frontend
npm install
npm run dev    # localhost:5173, proxied to backend at :8000
```

For a fully containerised local run:

```bash
docker build -t seedflip .
docker run --rm -p 8000:8000 -v $(pwd)/data:/data seedflip
```

The Dockerfile is multi-stage: it builds the React dashboard with Node 20,
installs Python deps with Poetry, and ships the combined artifact as a slim
Python image.

## API reference (relevant endpoints)

| method | path                              | purpose                                     |
|--------|-----------------------------------|---------------------------------------------|
| GET    | `/healthz`                        | Liveness probe                              |
| GET    | `/api/wallet`                     | Address, SOL/USDC/USDT balances, warnings   |
| GET    | `/api/state`                      | Bot mode, bankroll, deposits, etc.          |
| GET    | `/api/config`                     | Strategy params, target, horizon            |
| GET    | `/api/positions`                  | Open positions (with txids in live mode)    |
| GET    | `/api/trades`                     | Closed trades                               |
| POST   | `/api/control/halt`               | Stop opening new positions                  |
| POST   | `/api/control/resume`             | Resume in current mode                      |
| POST   | `/api/control/test-swap`          | $1 USDC→SOL→USDC smoke test                 |
| POST   | `/api/control/go-live`            | Flip from `paper` to `live`                 |
| POST   | `/api/control/withdraw-onchain`   | Sweep USDC (+SOL) to a destination address  |
| POST   | `/api/backtest/run`               | Honest n-sim distribution                   |

All `control` endpoints expect the boot confirmation token printed in the
runner logs at startup.

## Configuration

Environment variables (with defaults):

| var                       | default                          | purpose                                |
|---------------------------|----------------------------------|----------------------------------------|
| `SOLANA_RPC_URL`          | `https://api.mainnet-beta.solana.com` | RPC endpoint (use Helius/QuickNode!) |
| `WALLET_SECRET_PATH`      | `/data/wallet.json`              | Where the keypair is persisted         |
| `DATABASE_URL`            | `sqlite:////data/seedflip.db`    | SQLite DB                              |
| `LIVE_TRADING_ARMED`      | `false`                          | Hard gate; must be `true` to go live   |
| `BANKROLL_USD_INITIAL`    | `10`                             | Used for backtest sim                  |
| `TARGET_USD`              | `100`                            | Auto-halt target                       |
| `HORIZON_HOURS`           | `168`                            | 7 days                                 |
| `MAX_DRAWDOWN_PCT`        | `0.5`                            | Circuit breaker                        |
| `DAILY_LOSS_CAP_PCT`      | `0.3`                            | Circuit breaker                        |

## Tests

```bash
poetry run pytest -q
```

The suite covers the live execution paths
(`_execute_open_live`, `_execute_close_live`), the withdraw endpoint's gating
(armed, ack text, live-mode refusal), and the position model.

## Security

- `wallet.json` is `0600`-mode and never leaves the volume.
- Live trading is gated on three independent checks (env, runtime token,
  acknowledgement text) — accidental presses can't drain the wallet.
- Withdraw refuses while the bot is in live mode (you must halt first).
- No secrets are committed; all sensitive values come from `fly secrets`.

## License

Apache 2.0. See `LICENSE`.
