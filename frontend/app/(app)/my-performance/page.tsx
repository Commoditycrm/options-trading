"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Spinner } from "@/components/Spinner";

type Range = "7d" | "30d" | "90d" | "all";

interface Summary {
  realized_total: string;
  trade_count: number;
  win_count: number;
  loss_count: number;
  win_rate: number;
  avg_win: string | null;
  avg_loss: string | null;
  largest_win: string | null;
  largest_loss: string | null;
  profit_factor: number | null;
  equity: string;
}
interface DailyPoint { date: string; pnl: string; cumulative: string; trades: number; }
interface SymbolRow { symbol: string; pnl: string; trades: number; win_rate: number; }
interface RecentTrade { symbol: string; instrument_type: string; quantity: string; pnl: string; closed_at: string; }
interface Perf {
  range: Range;
  summary: Summary;
  daily: DailyPoint[];
  by_symbol: SymbolRow[];
  recent_trades: RecentTrade[];
}

const usd = (v: string | null | undefined) =>
  v == null || v === "" ? "—" : Number(v).toLocaleString(undefined, { style: "currency", currency: "USD" });
const signColor = (v: string | number | null | undefined) => {
  if (v == null || v === "") return "var(--muted)";
  const n = Number(v);
  return n > 0 ? "var(--good)" : n < 0 ? "var(--bad)" : "var(--muted)";
};

const RANGES: { v: Range; label: string }[] = [
  { v: "7d", label: "7D" }, { v: "30d", label: "30D" }, { v: "90d", label: "90D" }, { v: "all", label: "All" },
];

/** Tiny SVG equity curve of cumulative realized P&L. */
function EquityCurve({ daily }: { daily: DailyPoint[] }) {
  if (daily.length < 2) {
    return <p className="text-sm" style={{ color: "var(--muted)" }}>Not enough closed trades to chart yet.</p>;
  }
  const W = 720, H = 160, pad = 8;
  const vals = daily.map(d => Number(d.cumulative));
  const min = Math.min(0, ...vals), max = Math.max(0, ...vals);
  const span = max - min || 1;
  const x = (i: number) => pad + (i / (daily.length - 1)) * (W - 2 * pad);
  const y = (v: number) => H - pad - ((v - min) / span) * (H - 2 * pad);
  const path = vals.map((v, i) => `${i === 0 ? "M" : "L"} ${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  const last = vals[vals.length - 1];
  const zeroY = y(0);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: 160 }} preserveAspectRatio="none">
      <line x1={pad} y1={zeroY} x2={W - pad} y2={zeroY} stroke="var(--border)" strokeDasharray="3 3" />
      <path d={path} fill="none" stroke={last >= 0 ? "var(--good)" : "var(--bad)"} strokeWidth="2" />
    </svg>
  );
}

export default function MyPerformancePage() {
  const [range, setRange] = useState<Range>("30d");
  const [perf, setPerf] = useState<Perf | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      setPerf(await api<Perf>(`/api/trader/performance?range=${range}&tz=${encodeURIComponent(tz)}`));
    } catch { setPerf(null); }
    finally { setLoading(false); }
  }, [range]);

  useEffect(() => { load(); }, [load]);

  const s = perf?.summary;

  return (
    <div className="space-y-5 max-w-[1100px]">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold">My Trading Performance</h1>
          <p className="text-sm" style={{ color: "var(--muted)" }}>
            Your own realized P&amp;L, win rate, and per-symbol breakdown — independent of any copy fan-out.
          </p>
        </div>
        <div className="flex items-center gap-1 rounded border p-1" style={{ borderColor: "var(--border)" }}>
          {RANGES.map(r => (
            <button key={r.v} onClick={() => setRange(r.v)}
              className="px-3 py-1 text-sm rounded"
              style={range === r.v
                ? { background: "var(--accent)", color: "#06121f", fontWeight: 600 }
                : { color: "var(--muted)" }}>
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {loading && !perf ? (
        <p style={{ color: "var(--muted)" }}><span className="inline-flex items-center gap-2"><Spinner /> Loading…</span></p>
      ) : !s ? (
        <p style={{ color: "var(--muted)" }}>No performance data yet.</p>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Card label="Realized P&L" value={usd(s.realized_total)} color={signColor(s.realized_total)} />
            <Card label="Win rate" value={`${s.win_rate}%`} hint={`${s.win_count}W / ${s.loss_count}L of ${s.trade_count}`} />
            <Card label="Profit factor" value={s.profit_factor == null ? "—" : String(s.profit_factor)} hint="gross win ÷ gross loss" />
            <Card label="Account equity" value={usd(s.equity)} hint="connected brokers" />
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Card label="Avg win" value={usd(s.avg_win)} color="var(--good)" />
            <Card label="Avg loss" value={usd(s.avg_loss)} color="var(--bad)" />
            <Card label="Largest win" value={usd(s.largest_win)} color="var(--good)" />
            <Card label="Largest loss" value={usd(s.largest_loss)} color="var(--bad)" />
          </div>

          <section className="p-4 rounded border space-y-2" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
            <h2 className="font-medium">Equity curve <span className="text-xs" style={{ color: "var(--muted)" }}>(cumulative realized P&amp;L)</span></h2>
            <EquityCurve daily={perf!.daily} />
          </section>

          <div className="grid md:grid-cols-2 gap-4">
            <section className="p-4 rounded border space-y-2" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
              <h2 className="font-medium">By symbol</h2>
              {perf!.by_symbol.length === 0 ? (
                <p className="text-sm" style={{ color: "var(--muted)" }}>No closed trades in range.</p>
              ) : (
                <table className="w-full text-sm">
                  <thead><tr>{["Symbol", "P&L", "Trades", "Win %"].map(h => (
                    <th key={h} className="text-left px-2 py-1 font-medium" style={{ color: "var(--muted)" }}>{h}</th>))}</tr></thead>
                  <tbody>
                    {perf!.by_symbol.map(r => (
                      <tr key={r.symbol} className="border-t" style={{ borderColor: "var(--border)" }}>
                        <td className="px-2 py-1 font-medium">{r.symbol}</td>
                        <td className="px-2 py-1 num" style={{ color: signColor(r.pnl) }}>{usd(r.pnl)}</td>
                        <td className="px-2 py-1 num">{r.trades}</td>
                        <td className="px-2 py-1 num">{r.win_rate}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>

            <section className="p-4 rounded border space-y-2" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
              <h2 className="font-medium">Recent closed trades</h2>
              {perf!.recent_trades.length === 0 ? (
                <p className="text-sm" style={{ color: "var(--muted)" }}>No closed trades in range.</p>
              ) : (
                <table className="w-full text-sm">
                  <thead><tr>{["When", "Symbol", "Qty", "P&L"].map(h => (
                    <th key={h} className="text-left px-2 py-1 font-medium" style={{ color: "var(--muted)" }}>{h}</th>))}</tr></thead>
                  <tbody>
                    {perf!.recent_trades.map((t, i) => (
                      <tr key={i} className="border-t" style={{ borderColor: "var(--border)" }}>
                        <td className="px-2 py-1 num" style={{ color: "var(--muted)" }}>
                          {new Date(t.closed_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
                        </td>
                        <td className="px-2 py-1 font-medium">{t.symbol}</td>
                        <td className="px-2 py-1 num">{Number(t.quantity)}</td>
                        <td className="px-2 py-1 num" style={{ color: signColor(t.pnl) }}>{usd(t.pnl)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
          </div>
        </>
      )}
    </div>
  );
}

function Card({ label, value, color, hint }: { label: string; value: string; color?: string; hint?: string }) {
  return (
    <div className="p-4 rounded border" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
      <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>{label}</div>
      <div className="text-2xl font-semibold mt-1 num" style={{ color: color || "var(--text)" }}>{value}</div>
      {hint && <div className="text-[10px] mt-1" style={{ color: "var(--muted)" }}>{hint}</div>}
    </div>
  );
}
