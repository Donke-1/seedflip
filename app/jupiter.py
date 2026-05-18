"""Jupiter v6 aggregator client.

Builds, signs, and submits Solana swap transactions via the Jupiter HTTP API.
We use the public, unauthenticated endpoints:
  - GET  https://quote-api.jup.ag/v6/quote  -> route + price + slippage info
  - POST https://quote-api.jup.ag/v6/swap   -> serialized v0 transaction

The transaction returned by /swap already contains all required instructions
(create token account if missing, route swap, wrap/unwrap SOL, etc.) plus
appropriate compute-budget instructions when we ask for them. We sign it
locally with our keypair and submit via the RPC.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from .wallet import SOL_MINT, USDC_MINT, WalletManager

log = logging.getLogger(__name__)


JUPITER_BASE = "https://quote-api.jup.ag/v6"


@dataclass
class SwapResult:
    txid: str
    input_amount: int  # smallest units
    output_amount: int  # smallest units
    price_impact_pct: float
    confirmed: bool


class JupiterClient:
    def __init__(self, wallet: WalletManager, http: Optional[httpx.AsyncClient] = None):
        self.wallet = wallet
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ---- HTTP wrappers ------------------------------------------------------

    async def quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_smallest_units: int,
        slippage_bps: int = 150,
    ) -> dict:
        """GET /quote — returns the best route Jupiter found."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_smallest_units),
            "slippageBps": str(slippage_bps),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        r = await self._http.get(f"{JUPITER_BASE}/quote", params=params)
        if r.status_code != 200:
            raise RuntimeError(f"Jupiter quote failed {r.status_code}: {r.text}")
        return r.json()

    async def build_swap_tx(
        self,
        quote_response: dict,
        priority_fee_lamports: int = 5_000,
    ) -> str:
        """POST /swap — returns a base64-encoded serialized v0 transaction."""
        body = {
            "userPublicKey": self.wallet.pubkey_str(),
            "quoteResponse": quote_response,
            "wrapAndUnwrapSol": True,
            "useSharedAccounts": True,
            "computeUnitPriceMicroLamports": priority_fee_lamports,
            "dynamicComputeUnitLimit": True,
        }
        r = await self._http.post(f"{JUPITER_BASE}/swap", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"Jupiter swap build failed {r.status_code}: {r.text}")
        data = r.json()
        return data["swapTransaction"]

    # ---- sign + submit ------------------------------------------------------

    def _sign_serialized_tx(self, b64_tx: str, kp: Keypair) -> str:
        """Sign a base64 v0 transaction with our keypair and return base64."""
        raw = base64.b64decode(b64_tx)
        tx = VersionedTransaction.from_bytes(raw)
        # Recreate the signed transaction with our keypair.
        signed = VersionedTransaction(tx.message, [kp])
        return base64.b64encode(bytes(signed)).decode("ascii")

    async def execute_swap(
        self,
        input_mint: str,
        output_mint: str,
        amount_smallest_units: int,
        slippage_bps: int = 150,
        priority_fee_lamports: int = 5_000,
        wait_for_confirm: bool = True,
    ) -> SwapResult:
        """Full path: quote → build → sign → submit → (optionally) confirm."""
        q = await self.quote(input_mint, output_mint, amount_smallest_units, slippage_bps)
        b64_unsigned = await self.build_swap_tx(q, priority_fee_lamports=priority_fee_lamports)
        kp = self.wallet.load_or_create()
        b64_signed = self._sign_serialized_tx(b64_unsigned, kp)
        txid = await self.wallet.send_signed_b64_tx(b64_signed)
        confirmed = False
        if wait_for_confirm:
            confirmed = await self.wallet.confirm_transaction(txid, max_wait_s=90.0)
        return SwapResult(
            txid=txid,
            input_amount=int(q.get("inAmount", 0)),
            output_amount=int(q.get("outAmount", 0)),
            price_impact_pct=float(q.get("priceImpactPct", 0.0) or 0.0),
            confirmed=confirmed,
        )

    # ---- convenience wrappers ----------------------------------------------

    async def buy_usdc_to_token(self, output_mint: str, usdc_units: int, slippage_bps: int = 200) -> SwapResult:
        """Buy `output_mint` with `usdc_units` (6 decimals)."""
        return await self.execute_swap(USDC_MINT, output_mint, usdc_units, slippage_bps=slippage_bps)

    async def sell_token_to_usdc(self, input_mint: str, token_units: int, slippage_bps: int = 300) -> SwapResult:
        """Sell `token_units` of `input_mint` for USDC. Wider slippage because exit is on the way down by definition."""
        return await self.execute_swap(input_mint, USDC_MINT, token_units, slippage_bps=slippage_bps)

    async def roundtrip_test_usdc(self, usdc_units: int = 1_000_000) -> tuple[SwapResult, SwapResult]:
        """Forced $1 USDC → SOL → USDC round-trip used to verify the signing path.

        Returns (buy_result, sell_result). Both legs must confirm for the test to pass.
        """
        buy = await self.execute_swap(USDC_MINT, SOL_MINT, usdc_units, slippage_bps=200)
        if not buy.confirmed:
            return buy, SwapResult(txid="", input_amount=0, output_amount=0, price_impact_pct=0.0, confirmed=False)
        # Sell the SOL we just bought.
        sell = await self.execute_swap(SOL_MINT, USDC_MINT, buy.output_amount, slippage_bps=200)
        return buy, sell
