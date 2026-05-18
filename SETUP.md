# SeedFlip — self-hosting guide

This bundle contains everything needed to run the `$10 → $100 / 7-day` Solana
memecoin trading bot on a host of your choice. It is intentionally small so it
can run on the cheapest VPS or free PaaS tier.

## What this gives you

- A FastAPI backend with: real Solana keypair (kept on your host), Jupiter
  aggregator client (USDC ↔ SPL swaps), backtester, paper-trading runner,
  REST API.
- A bundled React dashboard, served by the backend at `/`. Same-origin = no
  CORS to configure.
- Persistent storage at `./data/`:
  - `wallet.json` — your Solana secret key, file permissions `0600`. **Do not
    commit, do not share.**
  - `seedflip.db` — SQLite database with bot state, trades, equity history.

## Important security notes

- The wallet keypair is **custodial on this host**. Whoever can read
  `data/wallet.json` can drain the funds. Treat the host like a hot wallet.
- **Keep the wallet bankroll small** ($10). If the host is compromised, the
  blast radius is capped at the deposited USDC + SOL.
- Always start with `LIVE_TRADING_ARMED=false`. Run the `$1` test-swap from
  the dashboard before flipping the arming flag.
- The honest-backtest distribution is unchanged: **P(hit $100) ≈ 14.4%, P(end
  ≤ $2) ≈ 54.0%, median $1.58**. Most runs do not hit $100. Do not deposit
  funds you cannot afford to lose.

## Hosting options

Any of these work. Pick the easiest one for you.

### Option A — Railway (~$5/mo, one-click) — recommended for non-ops folks

1. Push this directory to a new GitHub repo (or use Railway's "deploy from
   template" with a Docker source).
2. In Railway: `New Project → Deploy from GitHub repo`. Railway autodetects
   the `Dockerfile`.
3. In `Settings → Variables`, set:
   - `SOLANA_RPC_URL` (optional but recommended — see "Better RPC" below).
   - `LIVE_TRADING_ARMED=false` (start disarmed).
4. In `Settings → Volumes`, mount a volume at `/data` (1 GB is plenty).
5. Click `Deploy`. Railway gives you a public HTTPS URL.
6. Open the URL, you'll see the dashboard. The `Live wallet (on-chain)` card
   shows your newly-generated Solana address. **Back up the keypair** (see
   "Backing up the wallet" below) before sending any funds.

### Option B — Render free tier (free, sleeps after 15min idle)

Free tier sleeps when idle, so it's **not suitable for autonomous trading**.
Fine for a demo. For the real 7-day run, use the $7/mo "Starter" service tier
or one of the other options.

### Option C — Any Linux VPS (Hetzner, DO, Vultr, Linode — ~$4–5/mo)

```sh
# On the VPS, as a non-root user with docker + docker-compose installed:
git clone <your-fork-of-this-repo>.git seedflip
cd seedflip/seedflip_api
cp .env.example .env
# Edit .env: set SOLANA_RPC_URL, keep LIVE_TRADING_ARMED=false for now
mkdir -p data
docker compose up -d
docker compose logs -f seedflip   # watch the wallet address get generated
```

Then point a reverse proxy (Caddy, nginx) at port 8000 to give it TLS. Or use
Cloudflare Tunnel if you don't want to deal with DNS.

### Option D — Run on your own laptop (development only)

```sh
cd seedflip_api
poetry install
poetry run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then `http://localhost:8000`. Note: closing the laptop = bot stops trading.
Not suitable for the 7-day run.

## After it starts

1. Open the dashboard URL. The `Live wallet (on-chain)` card displays the
   newly-generated Solana address.
2. **Back up the keypair NOW**:
   ```sh
   docker compose exec seedflip cat /data/wallet.json
   ```
   Save this string somewhere safe (password manager). It's the base58 secret
   key. If the host dies, you can import this into a Solana wallet (Phantom:
   "Import private key" → paste it) to recover funds.
3. Send a **small test amount** to the address: $1.50 USDC (Solana SPL mint
   `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`) + ~$0.20 worth of SOL
   (~0.001 SOL on mainnet) for fees.
4. Get the go-live confirmation token from the logs:
   ```sh
   docker compose logs seedflip 2>&1 | grep "Go-live confirmation token"
   ```
   It looks like: `Go-live confirmation token: 1J103LyiovK2fHQE5PnTdA`.
5. Set `LIVE_TRADING_ARMED=true` in `.env` and restart:
   ```sh
   docker compose up -d --force-recreate seedflip
   ```
6. Refresh the dashboard. The test-swap section will become interactive.
7. Paste the token, click `Run test swap`. The page shows the buy and sell
   txids with links to Solscan. Both legs must confirm.
8. Only if step 7 succeeded: send the remaining USDC to make the full $10
   bankroll. Then in the `Arm autonomous live mode` section, paste the token,
   type the disclosure phrase, and click `Arm autonomous live trading`.

## Better RPC (strongly recommended)

The default `https://api.mainnet-beta.solana.com` is heavily rate-limited
(~10 req/s) and will throttle the bot. Get a free key from any of:

- [Helius](https://helius.dev) — free tier `https://mainnet.helius-rpc.com/?api-key=...`
- [QuickNode](https://quicknode.com) — free tier
- [Triton](https://triton.one)

Then put it in `.env`:

```
SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
```

## What the bot does (live mode)

When `mode=live`:

1. Polls real Solana memecoin signals (pump.fun migrations, Raydium fresh
   pairs, DexScreener fast-movers).
2. Filters out obvious rug-pulls (LP not locked, mint authority not renounced,
   dev wallet dumping, holder count too low). Tokens that fail any filter are
   skipped — the bot does NOT trade them.
3. If a token passes filters, builds a Jupiter quote (USDC → token), signs
   with the host's keypair, submits to your RPC, waits for confirmation.
4. Monitors position via DexScreener / Birdeye price feed. Exits on a
   take-profit ladder (2x → 50% out, 4x → another 25%, trailing stop on the
   rest) or a `-40%` hard stop.
5. Daily loss cutoff at `-40%` of session start. Halts and waits for manual
   resume.
6. Per-strategy capital allocation: `S1=30%` (momentum), `S2=70%` (migration),
   `S3=0%` (scalp) — matches the best-known honest-backtest config.

## Stopping the bot

```sh
# Halt without uninstalling — bot stops trading but keeps state:
curl -X POST https://your-host/api/control/halt

# Or kill the container entirely:
docker compose down
```

To completely sweep funds, import `wallet.json` into Phantom and send USDC +
SOL out to your CEX or another wallet.

## Updating

```sh
git pull
docker compose build seedflip
docker compose up -d
```

State in `./data/` survives the rebuild.

## Help / debugging

- Logs: `docker compose logs -f seedflip`
- Wallet status: `curl https://your-host/api/wallet`
- State: `curl https://your-host/api/state`
- Recent trades: `curl https://your-host/api/trades?limit=50`
- Equity history: `curl https://your-host/api/equity?limit=200`
- Force-halt: `curl -X POST https://your-host/api/control/halt`
- Force-resume: `curl -X POST https://your-host/api/control/resume`
