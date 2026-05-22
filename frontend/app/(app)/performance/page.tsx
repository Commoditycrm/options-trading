"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { useEventStream } from "@/lib/sse";
import { Spinner } from "@/components/Spinner";

interface FanoutRow {
  order_id: string;
  symbol: string;
  side: string;
  quantity: string;
  instrument_type: string;
  submitted_at: string | null;          // when Alpaca accepted (broker clock)
  detected_at: string | null;           // when we recorded the row (~detection)
  fanout_completed_at: string | null;   // last subscriber submit
  detection_lag_ms: number | null;
  fanout_duration_ms: number | null;
  total_ms: number | null;
  subscribers_targeted: number;
  subscribers_accepted: number;
  subscribers_rejected: number;
}

// Format milliseconds → "1.42s" or "245ms" depending on size.
function fmtMs(ms: number | null): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

// Colour-code a latency band. Green < 2s, amber 2-5s, red > 5s.
function lagColor(ms: number | null): string {
  if (ms === null || ms === undefined) return "var(--muted)";
  if (ms < 2000) return "var(--good)";
  if (ms < 5000) return "var(--amber, #d97706)";
  return "var(--bad)";
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false, fractionalSecondDigits: 3,
  });
}

export default function FanoutPerformancePage() {
  const [rows, setRows] = useState<FanoutRow[]>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    try {
      const r = await api<FanoutRow[]>("/api/trader/fanout-performance?limit=20");
      setRows(r);
    } catch { /* ignore — page renders empty */ }
    finally { setLoading(false); }
  }

  useEffect(() => {
    load();
    // Refresh every 5 seconds while the page is open so the demo feels live.
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  // Real-time refresh on any order event for this trader — saves the 5s
  // polling tick when a trade actually happens.
  useEventStream((evt) => {
    if (evt.type === "order.placed" || evt.type === "order.updated") {
      // Slight delay so child orders have time to update before we re-fetch.
      setTimeout(load, 1000);
    }
  });

  // Summary stats across all displayed fanouts.
  const stats = useMemo(() => {
    const withLag = rows.filter(r => r.fanout_duration_ms !== null);
    if (withLag.length === 0) return null;
    const fanoutDurations = withLag.map(r => r.fanout_duration_ms!);
    const totals = rows.filter(r => r.total_ms !== null).map(r => r.total_ms!);
    return {
      count: rows.length,
      avgFanout: Math.round(fanoutDurations.reduce((a, b) => a + b, 0) / fanoutDurations.length),
      maxFanout: Math.max(...fanoutDurations),
      avgTotal: totals.length ? Math.round(totals.reduce((a, b) => a + b, 0) / totals.length) : null,
      maxTotal: totals.length ? Math.max(...totals) : null,
    };
  }, [rows]);

  return (
    <div className="flex flex-col h-full max-w-7xl space-y-4">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold">Fanout Performance</h1>
        <p className="text-sm" style={{ color: "var(--muted)" }}>
          Latency breakdown for your most recent trades that fanned out to subscribers.
          Auto-refreshes every 5 seconds and on every new trade event.
        </p>
      </div>

      {/* Summary cards */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <SummaryCard label="Fanouts shown" value={String(stats.count)} />
          <SummaryCard
            label="Avg fanout time"
            value={fmtMs(stats.avgFanout)}
            color={lagColor(stats.avgFanout)}
          />
          <SummaryCard
            label="Max fanout time"
            value={fmtMs(stats.maxFanout)}
            color={lagColor(stats.maxFanout)}
          />
          <SummaryCard
            label="Avg total latency"
            value={fmtMs(stats.avgTotal)}
            color={lagColor(stats.avgTotal)}
            hint="Alpaca-accept → all subscribers submitted"
          />
        </div>
      )}

      {/* Table */}
      <div
        className="flex-1 min-h-0 overflow-auto rounded border"
        style={{ borderColor: "var(--border)" }}
      >
        <table className="min-w-full text-sm">
          <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
            <tr>
              {[
                "Symbol", "Side", "Qty",
                "Broker accepted at", "Detected at", "Fanout completed at",
                "Detection lag", "Fanout duration", "Total",
                "Subscribers",
              ].map(h => (
                <th key={h}
                    className="text-left px-4 py-3 font-medium whitespace-nowrap"
                    style={{ color: "var(--muted)" }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={10} className="px-3 py-8 text-center" style={{ color: "var(--muted)" }}>
                  <span className="inline-flex items-center gap-2">
                    <Spinner />
                    <span>Loading recent fanouts…</span>
                  </span>
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={10} className="px-3 py-6 text-center" style={{ color: "var(--muted)" }}>
                  No fanouts yet. Place a trade in Alpaca (or via Trade Panel) and it'll show up here within a couple seconds.
                </td>
              </tr>
            )}
            {rows.map(r => (
              <tr key={r.order_id} className="border-t" style={{ borderColor: "var(--border)" }}>
                <td className="px-4 py-3 font-medium whitespace-nowrap">{r.symbol}</td>
                <td className="px-4 py-3 uppercase font-medium"
                    style={{ color: r.side === "buy" ? "var(--good)" : "var(--bad)" }}>
                  {r.side}
                </td>
                <td className="px-4 py-3 num">{r.quantity}</td>
                <td className="px-4 py-3 num whitespace-nowrap" style={{ color: "var(--muted)" }}>
                  {fmtTime(r.submitted_at)}
                </td>
                <td className="px-4 py-3 num whitespace-nowrap" style={{ color: "var(--muted)" }}>
                  {fmtTime(r.detected_at)}
                </td>
                <td className="px-4 py-3 num whitespace-nowrap" style={{ color: "var(--muted)" }}>
                  {fmtTime(r.fanout_completed_at)}
                </td>
                <td className="px-4 py-3 num font-medium whitespace-nowrap"
                    style={{ color: lagColor(r.detection_lag_ms) }}>
                  {fmtMs(r.detection_lag_ms)}
                </td>
                <td className="px-4 py-3 num font-medium whitespace-nowrap"
                    style={{ color: lagColor(r.fanout_duration_ms) }}>
                  {fmtMs(r.fanout_duration_ms)}
                </td>
                <td className="px-4 py-3 num font-medium whitespace-nowrap"
                    style={{ color: lagColor(r.total_ms) }}>
                  {fmtMs(r.total_ms)}
                </td>
                <td className="px-4 py-3 whitespace-nowrap">
                  <span style={{ color: "var(--good)" }}>{r.subscribers_accepted} ✓</span>
                  {r.subscribers_rejected > 0 && (
                    <>
                      <span style={{ color: "var(--muted)" }}> / </span>
                      <span style={{ color: "var(--bad)" }}>{r.subscribers_rejected} ✗</span>
                    </>
                  )}
                  <span style={{ color: "var(--muted)" }}> of {r.subscribers_targeted}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="text-xs" style={{ color: "var(--muted)" }}>
        <strong>Detection lag</strong> = time between Alpaca accepting your order and our backend seeing it (limited by the 2-second poll interval today, ~100ms when the WebSocket is restored).
        {" "}<strong>Fanout duration</strong> = time from our detection to the last subscriber's order being accepted at their broker (parallel processing via Redis Streams + worker pool).
        {" "}<strong>Total</strong> = end-to-end (Alpaca-accept → last subscriber submitted).
      </p>
    </div>
  );
}

function SummaryCard({
  label, value, color, hint,
}: {
  label: string;
  value: string;
  color?: string;
  hint?: string;
}) {
  return (
    <div className="p-4 rounded border" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
      <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>
        {label}
      </div>
      <div className="text-2xl font-semibold mt-1 num"
           style={{ color: color || "var(--text)" }}>
        {value}
      </div>
      {hint && (
        <div className="text-[10px] mt-1" style={{ color: "var(--muted)" }}>{hint}</div>
      )}
    </div>
  );
}
