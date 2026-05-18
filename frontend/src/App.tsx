import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Banknote,
  CheckCircle2,
  Copy,
  ExternalLink,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  Rocket,
  ShieldAlert,
  Wallet,
} from "lucide-react";

const API = "/api";

type WalletStatus = {
  address: string;
  sol_balance: number;
  usdc_balance: number;
  usdt_balance: number;
  funded: boolean;
  armed_env: boolean;
  rpc_url: string;
  warning: string | null;
};

type State = {
  mode: string;
  bankroll_usd: number;
  bankroll_start_usd: number;
  deposits_usd: number;
  withdrawals_usd: number;
  halted_reason: string;
  session_start_at: string;
  last_tick_at: string | null;
  equity_usd: number;
  open_positions: Array<{
    symbol: string;
    address: string;
    strategy: string;
    size_usd: number;
    entry_price: number;
    opened_at_tick: number;
    max_seen_mult: number;
  }>;
  target_usd: number;
  horizon_days: number;
};

type Config = {
  live_trading_armed_env: boolean;
  strategy_params: Record<string, unknown>;
  headline_stats: {
    p_hit_target: number;
    p_near_zero: number;
    median_final: number;
    mean_final: number;
    p95_final: number;
  };
  go_live_token_hint: string;
};

type Trade = {
  id: number;
  opened_at: string;
  closed_at: string | null;
  strategy: string;
  symbol: string;
  size_usd: number;
  entry_price: number;
  exit_price: number | null;
  pnl_usd: number | null;
  pnl_pct: number | null;
  exit_reason: string | null;
  mode: string;
};

async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const j = await r.json();
      detail = j.detail || JSON.stringify(j);
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}

function fmtUsd(n: number | undefined | null): string {
  if (n === undefined || n === null || Number.isNaN(n)) return "—";
  return `$${n.toFixed(2)}`;
}

function fmtPct(n: number | undefined | null): string {
  if (n === undefined || n === null || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function Pill({ children, tone = "neutral" }: { children: React.ReactNode; tone?: "neutral" | "green" | "red" | "amber" }) {
  const toneClass = {
    neutral: "bg-zinc-700 text-zinc-200",
    green: "bg-emerald-700 text-emerald-100",
    red: "bg-rose-700 text-rose-100",
    amber: "bg-amber-700 text-amber-100",
  }[tone];
  return <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${toneClass}`}>{children}</span>;
}

function Section({ title, icon, children }: { title: string; icon?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-2xl border border-zinc-800 bg-zinc-900/80 p-5 shadow-sm">
      <h2 className="mb-3 flex items-center gap-2 text-base font-semibold text-zinc-100">
        {icon}
        <span>{title}</span>
      </h2>
      <div className="space-y-3 text-sm text-zinc-200">{children}</div>
    </section>
  );
}

export default function App() {
  const [wallet, setWallet] = useState<WalletStatus | null>(null);
  const [state, setState] = useState<State | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const [token, setToken] = useState("");
  const [riskText, setRiskText] = useState("");
  const [testSwapAmount, setTestSwapAmount] = useState(1);
  const [withdrawDest, setWithdrawDest] = useState("");
  const [withdrawAck, setWithdrawAck] = useState("");
  const [includeSolDust, setIncludeSolDust] = useState(true);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setErr(null);
      const [w, s, c, t] = await Promise.all([
        api<WalletStatus>("/wallet"),
        api<State>("/state"),
        api<Config>("/config"),
        api<Trade[]>("/trades?limit=25"),
      ]);
      setWallet(w);
      setState(s);
      setConfig(c);
      setTrades(t);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 10000);
    return () => clearInterval(id);
  }, [refresh]);

  const doAction = useCallback(
    async (label: string, fn: () => Promise<unknown>) => {
      setActionBusy(true);
      setActionMsg(null);
      try {
        await fn();
        setActionMsg(`${label} OK`);
        await refresh();
      } catch (e) {
        if (e instanceof Error && e.message === "__no_overwrite_msg__") {
          await refresh();
          return;
        }
        setActionMsg(`${label} failed: ${e instanceof Error ? e.message : String(e)}`);
      } finally {
        setActionBusy(false);
      }
    },
    [refresh],
  );

  const copyAddress = () => {
    if (!wallet) return;
    void navigator.clipboard.writeText(wallet.address);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const goLive = () =>
    doAction("Go Live", () =>
      api("/control/go-live", {
        method: "POST",
        body: JSON.stringify({ confirmation_token: token, acknowledge_risk_text: riskText }),
      }),
    );

  const halt = () => doAction("Halt", () => api("/control/halt", { method: "POST" }));
  const resume = () => doAction("Resume (paper)", () => api("/control/resume", { method: "POST" }));

  const testSwap = () =>
    doAction("Test swap", async () => {
      const out = await api<{
        ok: boolean;
        round_trip_cost_pct: number | null;
        output_usdc: number | null;
        buy_txid: string | null;
        sell_txid: string | null;
        error: string | null;
      }>("/control/test-swap", {
        method: "POST",
        body: JSON.stringify({ confirmation_token: token, usdc_amount: testSwapAmount }),
      });
      const cost = out.round_trip_cost_pct;
      const costStr = cost === null ? "?" : `${(cost * 100).toFixed(2)}%`;
      const outStr = out.output_usdc === null ? "?" : `$${out.output_usdc.toFixed(4)}`;
      setActionMsg(
        out.ok
          ? `Test swap OK: ${outStr} returned (round-trip cost ${costStr})`
          : `Test swap FAILED: ${out.error ?? "unknown"}`,
      );
      throw new Error("__no_overwrite_msg__");
    });

  const withdraw = () =>
    doAction("Withdraw on-chain", async () => {
      const out = await api<{
        ok: boolean;
        usdc_txid: string | null;
        usdc_amount: number;
        sol_txid: string | null;
        sol_amount: number;
        error: string | null;
      }>("/control/withdraw-onchain", {
        method: "POST",
        body: JSON.stringify({
          confirmation_token: token,
          destination_address: withdrawDest,
          include_sol_dust: includeSolDust,
          acknowledge_text: withdrawAck,
        }),
      });
      const parts = [
        `USDC: $${out.usdc_amount.toFixed(4)}${out.usdc_txid ? ` (tx ${out.usdc_txid.slice(0, 12)}…)` : ""}`,
        `SOL: ${out.sol_amount.toFixed(6)}${out.sol_txid ? ` (tx ${out.sol_txid.slice(0, 12)}…)` : ""}`,
      ];
      setActionMsg(
        out.ok ? `Withdraw OK — ${parts.join(", ")}` : `Withdraw failed: ${out.error ?? "see logs"}`,
      );
      throw new Error("__no_overwrite_msg__");
    });

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-zinc-950 text-zinc-200">
        <Loader2 className="mr-2 h-5 w-5 animate-spin" />
        Loading SeedFlip…
      </div>
    );
  }

  const modePill = (() => {
    if (!state) return null;
    const m = state.mode;
    if (m === "live") return <Pill tone="red">LIVE</Pill>;
    if (m === "halted") return <Pill tone="amber">HALTED</Pill>;
    return <Pill tone="green">PAPER</Pill>;
  })();

  return (
    <div className="min-h-screen bg-zinc-950 px-4 py-8 text-zinc-100">
      <div className="mx-auto flex max-w-5xl flex-col gap-6">
        <header className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">SeedFlip</h1>
            <p className="mt-1 text-sm text-zinc-400">
              Solana memecoin grow-$10-to-$100 bot. {modePill}{" "}
              {state?.last_tick_at && (
                <span className="ml-1 text-xs text-zinc-500">
                  last tick {new Date(state.last_tick_at).toLocaleTimeString()}
                </span>
              )}
            </p>
          </div>
          <button
            className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-2 text-sm hover:bg-zinc-800"
            onClick={() => void refresh()}
          >
            <RefreshCw className="h-4 w-4" /> Refresh
          </button>
        </header>

        {err && (
          <div className="rounded-lg border border-rose-700 bg-rose-950 p-3 text-sm text-rose-200">
            <AlertTriangle className="mr-2 inline h-4 w-4" />
            {err}
          </div>
        )}

        {wallet?.warning && (
          <div className="rounded-lg border border-amber-700 bg-amber-950 p-3 text-sm text-amber-200">
            <AlertTriangle className="mr-2 inline h-4 w-4" />
            {wallet.warning}
          </div>
        )}

        <Section title="Wallet" icon={<Wallet className="h-4 w-4" />}>
          {wallet ? (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <code className="rounded bg-zinc-800 px-2 py-1 text-xs">{wallet.address}</code>
                <button
                  className="inline-flex items-center gap-1 rounded border border-zinc-700 px-2 py-1 text-xs hover:bg-zinc-800"
                  onClick={copyAddress}
                >
                  <Copy className="h-3 w-3" /> {copied ? "Copied!" : "Copy"}
                </button>
                <a
                  href={`https://solscan.io/account/${wallet.address}`}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 rounded border border-zinc-700 px-2 py-1 text-xs hover:bg-zinc-800"
                >
                  <ExternalLink className="h-3 w-3" /> Solscan
                </a>
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <div className="rounded-lg bg-zinc-800/50 p-3">
                  <div className="text-xs text-zinc-400">SOL</div>
                  <div className="text-lg font-semibold">{wallet.sol_balance.toFixed(5)}</div>
                </div>
                <div className="rounded-lg bg-zinc-800/50 p-3">
                  <div className="text-xs text-zinc-400">USDC</div>
                  <div className="text-lg font-semibold">{wallet.usdc_balance.toFixed(2)}</div>
                </div>
                <div className="rounded-lg bg-zinc-800/50 p-3">
                  <div className="text-xs text-zinc-400">USDT</div>
                  <div className="text-lg font-semibold text-amber-300">{wallet.usdt_balance.toFixed(2)}</div>
                </div>
                <div className="rounded-lg bg-zinc-800/50 p-3">
                  <div className="text-xs text-zinc-400">Funded?</div>
                  <div className="text-lg font-semibold">{wallet.funded ? "Yes" : "No"}</div>
                </div>
              </div>
              <div className="text-xs text-zinc-400">
                Send <span className="font-mono text-emerald-300">USDC</span> on Solana to this address to deposit.
                Need ~0.01 SOL for fees. <strong className="text-rose-300">Do NOT send USDT</strong> — the bot only trades USDC.
              </div>
            </>
          ) : (
            <div className="text-zinc-400">Wallet not initialized.</div>
          )}
        </Section>

        <Section title="Bot State" icon={<Banknote className="h-4 w-4" />}>
          {state ? (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div className="rounded-lg bg-zinc-800/50 p-3">
                <div className="text-xs text-zinc-400">Equity</div>
                <div className="text-lg font-semibold">{fmtUsd(state.equity_usd)}</div>
              </div>
              <div className="rounded-lg bg-zinc-800/50 p-3">
                <div className="text-xs text-zinc-400">Bankroll (cash)</div>
                <div className="text-lg font-semibold">{fmtUsd(state.bankroll_usd)}</div>
              </div>
              <div className="rounded-lg bg-zinc-800/50 p-3">
                <div className="text-xs text-zinc-400">Deposits</div>
                <div className="text-lg font-semibold">{fmtUsd(state.deposits_usd)}</div>
              </div>
              <div className="rounded-lg bg-zinc-800/50 p-3">
                <div className="text-xs text-zinc-400">Withdrawals</div>
                <div className="text-lg font-semibold">{fmtUsd(state.withdrawals_usd)}</div>
              </div>
              <div className="col-span-2 rounded-lg bg-zinc-800/50 p-3 sm:col-span-4">
                <div className="text-xs text-zinc-400">Target / horizon</div>
                <div className="text-sm">
                  Grow to {fmtUsd(state.target_usd)} within {state.horizon_days} days.
                  {state.halted_reason && (
                    <span className="ml-2 text-rose-300">Halted: {state.halted_reason}</span>
                  )}
                </div>
              </div>
            </div>
          ) : (
            <div className="text-zinc-400">No state yet.</div>
          )}
          <div className="flex flex-wrap gap-2 pt-1">
            <button
              className="inline-flex items-center gap-1 rounded-lg bg-amber-700 px-3 py-2 text-sm font-medium hover:bg-amber-600 disabled:opacity-50"
              disabled={actionBusy}
              onClick={() => void halt()}
            >
              <Pause className="h-4 w-4" /> Halt
            </button>
            <button
              className="inline-flex items-center gap-1 rounded-lg bg-emerald-700 px-3 py-2 text-sm font-medium hover:bg-emerald-600 disabled:opacity-50"
              disabled={actionBusy}
              onClick={() => void resume()}
            >
              <Play className="h-4 w-4" /> Resume (paper)
            </button>
          </div>
        </Section>

        <Section title="Risk reality check (n=2000 sims)" icon={<ShieldAlert className="h-4 w-4" />}>
          {config ? (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-5 text-sm">
              <Stat label="P(hit target)" value={fmtPct(config.headline_stats.p_hit_target)} tone="green" />
              <Stat label="P(near zero)" value={fmtPct(config.headline_stats.p_near_zero)} tone="red" />
              <Stat label="Median final" value={fmtUsd(config.headline_stats.median_final)} />
              <Stat label="Mean final" value={fmtUsd(config.headline_stats.mean_final)} />
              <Stat label="P95 final" value={fmtUsd(config.headline_stats.p95_final)} />
            </div>
          ) : (
            <div className="text-zinc-400">No config.</div>
          )}
          <div className="rounded-lg border border-rose-800 bg-rose-950/40 p-3 text-xs text-rose-200">
            This is a speculative bot. Most outcomes are near-zero. Only deposit what you can afford to lose.
          </div>
        </Section>

        <Section title="Live trading gate" icon={<Rocket className="h-4 w-4" />}>
          <div className="text-xs text-zinc-400">
            LIVE_TRADING_ARMED env: {config?.live_trading_armed_env ? <Pill tone="green">true</Pill> : <Pill tone="red">false</Pill>}
            . Confirmation token is logged once at boot (look for "Go-live confirmation token" in fly logs).
          </div>
          <input
            className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
            placeholder="confirmation token"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-xs text-zinc-400">Test-swap $:</label>
            <input
              type="number"
              step="0.5"
              min="0.5"
              max="5"
              className="w-20 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs"
              value={testSwapAmount}
              onChange={(e) => setTestSwapAmount(Number(e.target.value))}
            />
            <button
              className="inline-flex items-center gap-1 rounded bg-sky-700 px-3 py-1 text-xs font-medium hover:bg-sky-600 disabled:opacity-50"
              disabled={actionBusy || !token}
              onClick={() => void testSwap()}
            >
              Test-swap USDC→SOL→USDC
            </button>
          </div>
          <input
            className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs"
            placeholder='Risk acknowledgement (e.g. "I understand this is a high-variance speculative bot...")'
            value={riskText}
            onChange={(e) => setRiskText(e.target.value)}
          />
          <button
            className="inline-flex items-center gap-1 rounded bg-rose-700 px-3 py-2 text-sm font-medium hover:bg-rose-600 disabled:opacity-50"
            disabled={actionBusy || !token || riskText.length < 20}
            onClick={() => void goLive()}
          >
            <Rocket className="h-4 w-4" /> Arm LIVE mode
          </button>
        </Section>

        <Section title="Withdraw on-chain" icon={<Banknote className="h-4 w-4" />}>
          <div className="text-xs text-zinc-400">
            Sweeps wallet USDC (and optionally leftover SOL beyond a ~0.003 SOL rent reserve) to any
            Solana address. <strong>Halt the bot first.</strong>
          </div>
          <input
            className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
            placeholder="destination Solana address"
            value={withdrawDest}
            onChange={(e) => setWithdrawDest(e.target.value)}
          />
          <input
            className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs"
            placeholder='Type "I AGREE TO WITHDRAW"'
            value={withdrawAck}
            onChange={(e) => setWithdrawAck(e.target.value)}
          />
          <label className="flex items-center gap-2 text-xs text-zinc-400">
            <input
              type="checkbox"
              checked={includeSolDust}
              onChange={(e) => setIncludeSolDust(e.target.checked)}
            />
            also sweep leftover SOL (keeps 0.003 SOL for rent)
          </label>
          <button
            className="inline-flex items-center gap-1 rounded bg-emerald-700 px-3 py-2 text-sm font-medium hover:bg-emerald-600 disabled:opacity-50"
            disabled={actionBusy || !token || !withdrawDest || withdrawAck.length < 10}
            onClick={() => void withdraw()}
          >
            <Banknote className="h-4 w-4" /> Withdraw to address
          </button>
        </Section>

        {actionMsg && (
          <div className="rounded-lg border border-zinc-700 bg-zinc-900 p-3 text-sm">
            <CheckCircle2 className="mr-2 inline h-4 w-4 text-emerald-400" />
            {actionMsg}
          </div>
        )}

        <Section title="Open positions">
          {state && state.open_positions.length > 0 ? (
            <table className="w-full text-xs">
              <thead className="text-zinc-400">
                <tr>
                  <th className="text-left">Sym</th>
                  <th className="text-left">Strat</th>
                  <th className="text-right">Size $</th>
                  <th className="text-right">Entry</th>
                  <th className="text-right">Max mult</th>
                </tr>
              </thead>
              <tbody>
                {state.open_positions.map((p) => (
                  <tr key={p.address}>
                    <td className="font-medium">{p.symbol}</td>
                    <td>{p.strategy}</td>
                    <td className="text-right">{p.size_usd.toFixed(2)}</td>
                    <td className="text-right">{p.entry_price.toExponential(3)}</td>
                    <td className="text-right">{p.max_seen_mult.toFixed(2)}x</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="text-zinc-500">No open positions.</div>
          )}
        </Section>

        <Section title="Recent trades">
          {trades.length > 0 ? (
            <table className="w-full text-xs">
              <thead className="text-zinc-400">
                <tr>
                  <th className="text-left">When</th>
                  <th className="text-left">Strat</th>
                  <th className="text-left">Sym</th>
                  <th className="text-right">Size</th>
                  <th className="text-right">PnL</th>
                  <th className="text-left">Why</th>
                  <th className="text-left">Mode</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id} className="border-t border-zinc-800">
                    <td>{new Date(t.opened_at).toLocaleTimeString()}</td>
                    <td>{t.strategy}</td>
                    <td>{t.symbol}</td>
                    <td className="text-right">${t.size_usd.toFixed(2)}</td>
                    <td className={`text-right ${(t.pnl_usd ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
                      {t.pnl_usd === null ? "—" : `${t.pnl_usd >= 0 ? "+" : ""}${t.pnl_usd.toFixed(2)}`}
                    </td>
                    <td>{t.exit_reason ?? "open"}</td>
                    <td>{t.mode}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="text-zinc-500">No trades yet.</div>
          )}
        </Section>

        <footer className="pb-6 text-center text-xs text-zinc-600">
          SeedFlip · self-hosted · {wallet?.rpc_url}
        </footer>
      </div>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "green" | "red" }) {
  const valueTone = tone === "green" ? "text-emerald-300" : tone === "red" ? "text-rose-300" : "text-zinc-100";
  return (
    <div className="rounded-lg bg-zinc-800/50 p-3">
      <div className="text-xs text-zinc-400">{label}</div>
      <div className={`text-lg font-semibold ${valueTone}`}>{value}</div>
    </div>
  );
}
