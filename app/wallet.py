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
from solders.compute_budget import set_compute_unit_price
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams as SysTransferParams
from solders.system_program import transfer as sys_transfer
from solders.transaction import VersionedTransaction
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    TransferCheckedParams,
    create_associated_token_account,
    get_associated_token_address,
    transfer_checked,
)

log = logging.getLogger(__name__)


# Minimum SOL to keep in the wallet after a withdraw, to stay rent-exempt and
# leave enough lamports for one or two more transactions (fees + closing ATAs).
WITHDRAW_SOL_RESERVE: float = 0.003


# Well-known SPL mints on Solana mainnet.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
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

    async def get_token_decimals(self, mint: str) -> int:
        """Return the SPL token mint's decimals. Cached after first lookup."""
        cache = getattr(self, "_decimals_cache", None)
        if cache is None:
            cache = {}
            self._decimals_cache = cache
        if mint in cache:
            return cache[mint]
        # Hard-code well-known mints to save an RPC call (USDC/USDT/SOL = 6/6/9).
        hard_coded = {USDC_MINT: 6, USDT_MINT: 6, SOL_MINT: 9}
        if mint in hard_coded:
            cache[mint] = hard_coded[mint]
            return cache[mint]
        result = await self._rpc("getTokenSupply", [mint])
        decimals = int((result.get("value") or {}).get("decimals") or 6)
        cache[mint] = decimals
        return decimals

    async def get_spl_token_amount_raw(self, mint: str) -> int:
        """SPL token balance in smallest units (no decimals applied)."""
        result = await self._rpc(
            "getTokenAccountsByOwner",
            [self.pubkey_str(), {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        accounts = result.get("value", [])
        total = 0
        for acc in accounts:
            try:
                amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
                total += int(amt)
            except (KeyError, TypeError, ValueError):
                continue
        return total

    async def has_token_account(self, owner: str, mint: str) -> bool:
        """Cheaper than full balance query: does the owner have an ATA for this mint?"""
        result = await self._rpc(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        return bool(result.get("value"))

    async def get_balances(self) -> dict:
        sol, usdc, usdt = await asyncio.gather(
            self.get_sol_balance(),
            self.get_spl_token_balance(USDC_MINT),
            self.get_spl_token_balance(USDT_MINT),
        )
        return {"sol": sol, "usdc": usdc, "usdt": usdt, "address": self.pubkey_str()}

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

    async def _get_recent_blockhash(self) -> Hash:
        result = await self._rpc("getLatestBlockhash", [{"commitment": "finalized"}])
        bh = (result.get("value") or {}).get("blockhash")
        if not bh:
            raise RuntimeError("getLatestBlockhash returned no blockhash")
        return Hash.from_string(bh)

    async def transfer_spl_to(
        self,
        mint: str,
        destination_owner: str,
        amount_smallest_units: int,
        priority_fee_micro_lamports: int = 5_000,
    ) -> tuple[str, bool]:
        """Send `amount_smallest_units` of `mint` to the wallet `destination_owner`.

        Auto-creates the destination's associated token account if missing
        (the sender pays the ~0.002 SOL rent).

        Returns (txid, confirmed).
        """
        if amount_smallest_units <= 0:
            raise ValueError("amount must be > 0")

        kp = self.load_or_create()
        sender = kp.pubkey()
        dest_owner_pk = Pubkey.from_string(destination_owner)
        mint_pk = Pubkey.from_string(mint)

        src_ata = get_associated_token_address(sender, mint_pk)
        dst_ata = get_associated_token_address(dest_owner_pk, mint_pk)

        decimals = await self.get_token_decimals(mint)
        instructions = [
            set_compute_unit_price(priority_fee_micro_lamports),
        ]
        if not await self.has_token_account(destination_owner, mint):
            log.info("Destination ATA missing for %s; including create instruction", destination_owner)
            instructions.append(
                create_associated_token_account(payer=sender, owner=dest_owner_pk, mint=mint_pk)
            )
        instructions.append(
            transfer_checked(
                TransferCheckedParams(
                    program_id=TOKEN_PROGRAM_ID,
                    source=src_ata,
                    mint=mint_pk,
                    dest=dst_ata,
                    owner=sender,
                    amount=amount_smallest_units,
                    decimals=decimals,
                    signers=[],
                )
            )
        )

        blockhash = await self._get_recent_blockhash()
        msg = MessageV0.try_compile(
            payer=sender,
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=blockhash,
        )
        tx = VersionedTransaction(msg, [kp])
        b64 = base64.b64encode(bytes(tx)).decode("ascii")
        txid = await self.send_signed_b64_tx(b64)
        confirmed = await self.confirm_transaction(txid, max_wait_s=90.0)
        return txid, confirmed

    async def transfer_sol_to(
        self,
        destination_owner: str,
        lamports: int,
        priority_fee_micro_lamports: int = 5_000,
    ) -> tuple[str, bool]:
        """Native SOL transfer to a system-account destination.

        Returns (txid, confirmed).
        """
        if lamports <= 0:
            raise ValueError("lamports must be > 0")
        kp = self.load_or_create()
        sender = kp.pubkey()
        dest_pk = Pubkey.from_string(destination_owner)
        instructions = [
            set_compute_unit_price(priority_fee_micro_lamports),
            sys_transfer(SysTransferParams(from_pubkey=sender, to_pubkey=dest_pk, lamports=lamports)),
        ]
        blockhash = await self._get_recent_blockhash()
        msg = MessageV0.try_compile(
            payer=sender,
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=blockhash,
        )
        tx = VersionedTransaction(msg, [kp])
        b64 = base64.b64encode(bytes(tx)).decode("ascii")
        txid = await self.send_signed_b64_tx(b64)
        confirmed = await self.confirm_transaction(txid, max_wait_s=90.0)
        return txid, confirmed

    async def aclose(self) -> None:
        await self._http.aclose()


# Module-level singleton; FastAPI lifespan initializes it.
WALLET: Optional[WalletManager] = None
