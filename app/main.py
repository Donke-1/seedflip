"""SeedFlip API entrypoint.

DO NOT remove the CORS middleware below or rename this module / `app` global --
the deployment server depends on both.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pathlib import Path

from . import runner as runner_mod
from . import wallet as wallet_mod
from .api.routes import router
from .config import settings
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Initialize wallet (loads existing keypair from /data/wallet.json or generates).
    secret_path = Path(settings.wallet_secret_path) if settings.wallet_secret_path else None
    wallet_mod.WALLET = wallet_mod.WalletManager(secret_path=secret_path, rpc_url=settings.solana_rpc_url)
    wallet_mod.WALLET.load_or_create()
    log.info("Wallet pubkey: %s", wallet_mod.WALLET.pubkey_str())
    runner_mod.RUNNER = runner_mod.Runner()
    await runner_mod.RUNNER.start()
    log.info("SeedFlip started; paper-trading runner running.")
    log.info("Go-live confirmation token: %s", runner_mod.RUNNER.go_live_token)
    try:
        yield
    finally:
        if runner_mod.RUNNER:
            await runner_mod.RUNNER.stop()
        if wallet_mod.WALLET:
            await wallet_mod.WALLET.aclose()
        log.info("SeedFlip shutdown complete.")


app = FastAPI(title="SeedFlip", version="0.1.0", lifespan=lifespan)

# Disable CORS. Do not remove this for full-stack development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# All JSON API routes are mounted under /api so the static frontend (served
# below) can use relative paths and avoid CORS entirely.
app.include_router(router, prefix="/api")

# Serve the built React frontend from /. Optional — only mounted if the
# `dist/` directory has been copied in at deploy time.
_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "dist"
if _FRONTEND_DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
    log.info("Serving bundled frontend from %s", _FRONTEND_DIST)
else:
    log.info("No frontend bundle found at %s; serving API only", _FRONTEND_DIST)
