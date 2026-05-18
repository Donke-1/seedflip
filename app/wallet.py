"""Solana wallet manager.

Generates and persists a Solana keypair to the backend's persistent volume.
The secret key is written to disk ONCE on first start (or recovered from disk
on subsequent starts). Only the public address is ever exposed via the API.

We deliberately do NOT store the secret in the database (so it's not exported
in backups), in env vars (so it's not in deployment dashboards / logs), or in
git (the persistence path is under /data, which is mounted as a Fly volume).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional

import base58
import httpx
from solders.keypair import Keypair
from solders.pubkey import Pubkey

log = logging.getLogger(__name__)


# Well-known SPL mints on Solana mainnet.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"  # WSOL


def _default_wallet_path() -> Path:
    """Pick a writable persistent location for the wallet secret."""
    if Path("/data").is_dir() and os.access("/data", os.W_OK):
        return Path("/data/wallet.json")
    # Local development fallback.
    p = Path(__file__).resolve().parent.parent / "data" / "wallet.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class WalletManager:
    def __init__(self, secret_path: Optional[Path] = None, rpc_url: str = "https://api.mainnet-beta.solana.com"):
        self.secret_path = secret_path or _default_wallet_path()
        self.rpc_url = rpc_url
        self._keypair: Optional[Keypair] = None
        self._http = httpx.AsyncClient(timeout=30.0)

    # ---- keypair lifecycle --------------------------------------------------

    def load_or_create(self) -> Keypair:
        """Load the keypair from disk; if missing, generate a fresh one.

        The file format is a single line of base58-encoded 64-byte secret key
        (the Solana CLI / Phantom interchange format). Written with 0o600 perms.
        """
        if self._keypair is not None:
            return self._keypair

        if self.secret_path.exists():
            raw = self.secret_path.read_text().strip()
            try:
                # Try base58 (Phantom-compatible) first.
                secret = base58.b58decode(raw)
                kp = Keypair.from_bytes(secret)
            except Exception:  # noqa: BLE001
                # Fall back to JSON array of ints (Solana CLI format).
                arr = json.loads(raw)
                kp = Keypair.from_bytes(bytes(arr))
            self._keypair = kp
            log.info("Loaded existing wallet: %s", kp.pubkey())
            return kp

        kp = Keypair()
        self._save_keypair(kp)
        self._keypair = kp
        log.warning("Generated NEW wallet: %s (secret persisted to %s)", kp.pubkey(), self.secret_path)
        return kp

    def _save_keypair(self, kp: Keypair) -> None:
        """Persist the keypair to disk as base58."""
        raw = bytes(kp)  # 64 bytes
        b58 = base58.b58encode(raw).decode("ascii")
        self.secret_path.write_text(b58)
        try:
            os.chmod(self.secret_path, 0o600)
        except OSError:
            pass

    def pubkey(self) -> Pubkey:
        return self.load_or_create().pubkey()

    def pubkey_str(self) -> str:
        return str(self.pubkey())

    # ---- RPC helpers --------------------------------------------------------

    async def _rpc(self, method: str, params: list) -> dict:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        r = await self._http.post(self.rpc_url, json=payload)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"RPC {method} error: {data['error']}")
        return data.get("result", {})

    async def get_sol_balance(self) -> float:
        """SOL balance in whole SOL."""
        result = await self._rpc("getBalance", [self.pubkey_str()])
        lamports = result.get("value", 0)
        return lamports / 1e9

    async def get_spl_token_balance(self, mint: str) -> float:
        """SPL token balance (in display units, decimals applied)."""
        result = await self._rpc(
            "getTokenAccountsByOwner",
            [self.pubkey_str(), {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        accounts = result.get("value", [])
        total = 0.0
        for acc in accounts:
            try:
                amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
                if amt is not None:
                    total += float(amt)
            except (KeyError, TypeError, ValueError):
                continue
        return total

    async def get_balances(self) -> dict:
        sol, usdc = await asyncio.gather(
            self.get_sol_balance(),
            self.get_spl_token_balance(USDC_MINT),
        )
        return {"sol": sol, "usdc": usdc, "address": self.pubkey_str()}

    async def send_signed_b64_tx(self, b64_tx: str) -> str:
        """Send a fully-signed serialized transaction (base64) to the RPC.

        Returns the txid string.
        """
        result = await self._rpc(
            "sendTransaction",
            [b64_tx, {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed", "maxRetries": 3}],
        )
        if isinstance(result, str):
            return result
        # Some RPCs wrap result in a dict.
        return result if isinstance(result, str) else str(result)

    async def confirm_transaction(self, txid: str, max_wait_s: float = 60.0) -> bool:
        """Poll getSignatureStatuses until the tx is confirmed or timeout."""
        deadline = asyncio.get_event_loop().time() + max_wait_s
        while asyncio.get_event_loop().time() < deadline:
            try:
                result = await self._rpc("getSignatureStatuses", [[txid], {"searchTransactionHistory": True}])
                values = result.get("value", [])
                if values and values[0] is not None:
                    status = values[0]
                    if status.get("err") is not None:
                        log.error("tx %s failed: %s", txid, status["err"])
                        return False
                    conf = status.get("confirmationStatus")
                    if conf in ("confirmed", "finalized"):
                        return True
            except Exception as e:  # noqa: BLE001
                log.warning("confirm poll error: %s", e)
            await asyncio.sleep(1.0)
        log.warning("tx %s did not confirm within %.0fs", txid, max_wait_s)
        return False

    async def aclose(self) -> None:
        await self._http.aclose()


# Module-level singleton; FastAPI lifespan initializes it.
WALLET: Optional[WalletManager] = None
