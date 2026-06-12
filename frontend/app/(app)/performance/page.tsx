"use client";

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useEventStream } from "@/lib/sse";
import { Spinner } from "@/components/Spinner";

interface SubscriberRow {
  child_order_id: string;
  user_id: string;
  email: string | null;
  display_name: string | null;
  status: string;
  broker_order_id: string | null;
  quantity: string;
  filled_quantity: string;
  child_created_at: string | null;
  child_submitted_at: string | null;
  subscriber_lag_ms: number | null;   // parent.detected_at → child.submitted_at
  broker_ms: number | null;           // the subscriber's broker round-trip
  platform_ms: number | null;         // our overhead = total − broker
  total_ms: number | null;            // published → this subscriber submitted
  reject_reason: string | null;
}

interface FanoutRow {
  order_id: string;
  symbol: string;
  side: string;
  quantity: string;
  instrument_type: string;
  trader_submitted_at: string | null;
  broker_accepted_at: string | null;
  detected_at: string | null;
  fanout_published_at: string | null;
  all_subs_completed_at: string | null;
  api_broker_lag_ms: number | null;
  detection_lag_ms: number | null;
  platform_lag_ms: number | null;      // median per-subscriber platform overhead
  total_ms: number | null;
  median_total_ms: number | null;
  slowest_total_ms: number | null;
  within_1s_count: number;
  broker_lag_min_ms: number | null;
  broker_lag_avg_ms: number | null;
  broker_lag_max_ms: number | null;
  broker_lag_median_ms: number | null;
  subscribers_targeted: number;
  subscribers_accepted: number;
  subscribers_rejected: number;
  subscribers: SubscriberRow[];
}

function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}
// green if fast, amber, red. Tuned to sub-second goals.
function lagColor(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "var(--muted)";
  if (ms < 1000) return "var(--good)";
  if (ms < 2500) return "var(--amber, #d97706)";
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

function statusBadge(status: string) {
  const map: Record<string, { bg: string; color: string }> = {
    filled:            { bg: "var(--good-soft)",       color: "var(--good)"  },
    submitted:         { bg: "rgba(10,115,168,0.10)",  color: "var(--accent)" },
    accepted:          { bg: "rgba(10,115,168,0.10)",  color: "var(--accent)" },
    partially_filled:  { bg: "rgba(10,115,168,0.10)",  color: "var(--accent)" },
    pending:           { bg: "rgba(255,255,255,0.04)", color: "var(--muted)" },
    rejected:          { bg: "var(--bad-soft)",        color: "var(--bad)"   },
    canceled:          { bg: "rgba(255,255,255,0.04)", color: "var(--muted)" },
    expired:           { bg: "rgba(255,255,255,0.04)", color: "var(--muted)" },
  };
  const s = map[status] || { bg: "rgba(255,255,255,0.04)", color: "var(--muted)" };
  return (
    <span className="text-[11px] uppercase tracking-wider px-2 py-[3px] rounded whitespace-nowrap font-medium"
          style={{ background: s.bg, color: s.color }}>
      {status.replace("_", " ")}
    </span>
  );
}

export default function FanoutPerformancePage() {
  const [rows, setRows] = useState<FanoutRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Client-measured UI-notification lag: when the browser first received the
  // SSE for an order, vs the broker-accept timestamp in that event. Survives
  // across reloads only for trades seen live this session.
  const uiLag = useRef<Map<string, number>>(new Map());
  const [, force] = useState(0);

  async function load() {
    try {
      const r = await api<FanoutRow[]>("/api/trader/fanout-performance?limit=20");
      setRows(r);
    } catch { /* render empty */ }
    finally { setLoading(false); }
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  useEventStream((evt) => {
    if (evt.type === "order.placed" || evt.type === "order.updated") {
      // Record UI-notification lag = now − broker submitted_at (from the event).
      const o = evt.order;
      if (o?.id && o.created_at && !uiLag.current.has(o.id)) {
        const lag = Date.now() - new Date(o.created_at).getTime();
        if (lag >= 0 && lag < 120000) { uiLag.current.set(o.id, lag); force(n => n + 1); }
      }
      setTimeout(load, 1000);
    }
  });

  // Aggregate cards across the shown trades.
  const stats = useMemo(() => {
    if (rows.length === 0) return null;
    const totals = rows.map(r => r.median_total_ms).filter((x): x is number => x !== null);
    const brokers = rows.map(r => r.broker_lag_median_ms).filter((x): x is number => x !== null);
    const plats = rows.map(r => r.platform_lag_ms).filter((x): x is number => x !== null);
    const avg = (xs: number[]) => xs.length ? Math.round(xs.reduce((a, b) => a + b, 0) / xs.length) : null;
    return { count: rows.length, total: avg(totals), broker: avg(brokers), platform: avg(plats) };
  }, [rows]);

  function toggle(id: string) {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  const COLS = [
    "Symbol", "Side", "Qty",
    "Trader submitted at", "Broker accepted at", "Published for subs at", "All subs completed at",
    "API→broker lag", "UI notif lag", "Detection lag", "Platform lag", "Total",
    "Lowest broker", "Avg broker", "Highest broker", "Subscribers",
  ];

  return (
    <div className="flex flex-col h-full max-w-[1600px] space-y-4">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold">Fanout Performance</h1>
        <p className="text-sm" style={{ color: "var(--muted)" }}>
          Per-trade latency breakdown for your fan-outs, split into <strong>platform</strong> (our
          overhead) vs <strong>broker</strong> (the broker round-trip). Click a row for per-subscriber
          timing. Auto-refreshes every 5s and on each trade event.
        </p>
      </div>

      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <SummaryCard label="Fanouts shown" value={String(stats.count)} />
          <SummaryCard label="Avg total (per sub)" value={fmtMs(stats.total)} color={lagColor(stats.total)}
            hint="median per-subscriber, averaged" />
          <SummaryCard label="Avg platform lag" value={fmtMs(stats.platform)} color={lagColor(stats.platform)}
            hint="our overhead (no broker)" />
          <SummaryCard label="Avg broker lag" value={fmtMs(stats.broker)} color={lagColor(stats.broker)}
            hint="broker round-trip (not ours)" />
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-auto rounded border" style={{ borderColor: "var(--border)" }}>
        <table className="min-w-full text-sm">
          <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
            <tr>
              <th className="w-8" />
              {COLS.map(h => (
                <th key={h} className="text-left px-3 py-3 font-medium whitespace-nowrap"
                    style={{ color: "var(--muted)" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr><td colSpan={COLS.length + 1} className="px-3 py-8 text-center" style={{ color: "var(--muted)" }}>
                <span className="inline-flex items-center gap-2"><Spinner /><span>Loading recent fanouts…</span></span>
              </td></tr>
            )}
            {!loading && rows.length === 0 && (
              <tr><td colSpan={COLS.length + 1} className="px-3 py-6 text-center" style={{ color: "var(--muted)" }}>
                No fanouts yet. Place a trade and it&apos;ll show up here within a couple seconds.
              </td></tr>
            )}
            {rows.map(r => {
              const isOpen = expanded.has(r.order_id);
              const uiNotif = uiLag.current.get(r.order_id) ?? null;
              return (
                <Fragment key={r.order_id}>
                  <tr className="border-t cursor-pointer" style={{ borderColor: "var(--border)" }} onClick={() => toggle(r.order_id)}>
                    <td className="px-2 py-3 text-center" style={{ color: "var(--muted)" }}>{isOpen ? "▾" : "▸"}</td>
                    <td className="px-3 py-3 font-medium whitespace-nowrap">{r.symbol}</td>
                    <td className="px-3 py-3 uppercase font-medium" style={{ color: r.side === "buy" ? "var(--good)" : "var(--bad)" }}>{r.side}</td>
                    <td className="px-3 py-3 num">{r.quantity}</td>
                    <Tcell t={r.trader_submitted_at} />
                    <Tcell t={r.broker_accepted_at} />
                    <Tcell t={r.fanout_published_at} />
                    <Tcell t={r.all_subs_completed_at} />
                    <Lcell ms={r.api_broker_lag_ms} />
                    <Lcell ms={uiNotif} />
                    <Lcell ms={r.detection_lag_ms} />
                    <Lcell ms={r.platform_lag_ms} />
                    <Lcell ms={r.median_total_ms} bold />
                    <Lcell ms={r.broker_lag_min_ms} />
                    <Lcell ms={r.broker_lag_avg_ms} />
                    <Lcell ms={r.broker_lag_max_ms} />
                    <td className="px-3 py-3 whitespace-nowrap">
                      <span style={{ color: "var(--good)" }}>{r.subscribers_accepted} ✓</span>
                      {r.subscribers_rejected > 0 && (<>
                        <span style={{ color: "var(--muted)" }}> / </span>
                        <span style={{ color: "var(--bad)" }}>{r.subscribers_rejected} ✗</span>
                      </>)}
                      <span style={{ color: "var(--muted)" }}> of {r.subscribers_targeted}</span>
                    </td>
                  </tr>
                  {isOpen && (
                    <tr style={{ background: "var(--bg)" }}>
                      <td colSpan={COLS.length + 1} className="px-4 pt-2 pb-4">
                        <TradeSummary r={r} />
                        <SubscriberDetail subscribers={r.subscribers} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      <p className="text-xs" style={{ color: "var(--muted)" }}>
        <strong>API→broker lag</strong> = the trader&apos;s own broker round-trip.{" "}
        <strong>Detection lag</strong> = broker-accept → our backend seeing the order (≈0 for in-app trades; up to 2s on the external poller).{" "}
        <strong>Platform lag</strong> = our overhead per subscriber (queue + gates + write), excluding the broker.{" "}
        <strong>Broker lag</strong> = the subscriber&apos;s broker round-trip (not ours to shrink).{" "}
        <strong>Total</strong> = published-for-subs → subscriber submitted (median).{" "}
        <strong>UI notif lag</strong> = client-measured time until your browser received the SSE update (live trades only).
      </p>
    </div>
  );
}

function Tcell({ t }: { t: string | null }) {
  return <td className="px-3 py-3 num whitespace-nowrap" style={{ color: "var(--muted)" }}>{fmtTime(t)}</td>;
}
function Lcell({ ms, bold }: { ms: number | null; bold?: boolean }) {
  return (
    <td className={"px-3 py-3 num whitespace-nowrap" + (bold ? " font-semibold" : " font-medium")}
        style={{ color: lagColor(ms) }}>
      {fmtMs(ms)}
    </td>
  );
}

function TradeSummary({ r }: { r: FanoutRow }) {
  const placed = r.subscribers_accepted;
  const targeted = r.subscribers_targeted;
  const pct = targeted > 0 ? Math.round((r.within_1s_count / targeted) * 100) : 0;
  return (
    <div className="mb-3 p-3 rounded" style={{ background: "rgba(255,255,255,0.02)", border: "1px solid var(--border)" }}>
      <div className="text-[11px] uppercase tracking-wider mb-1" style={{ color: "var(--muted)" }}>Trade summary</div>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
        <span><strong style={{ color: "var(--good)" }}>{r.within_1s_count} of {targeted}</strong> subscribers placed within 1 second ({pct}%)</span>
        <span style={{ color: "var(--muted)" }}>Median: <strong style={{ color: "var(--text)" }}>{fmtMs(r.median_total_ms)}</strong></span>
        <span style={{ color: "var(--muted)" }}>Slowest: <strong style={{ color: "var(--text)" }}>{fmtMs(r.slowest_total_ms)}</strong></span>
        <span style={{ color: "var(--muted)" }}>{placed} accepted</span>
      </div>
      <div className="text-[11px] uppercase tracking-wider mt-2 mb-1" style={{ color: "var(--muted)" }}>Median lag split</div>
      <div className="flex flex-wrap gap-x-6 text-sm">
        <span>Total: <strong style={{ color: lagColor(r.median_total_ms) }}>{fmtMs(r.median_total_ms)}</strong></span>
        <span>Platform: <strong style={{ color: lagColor(r.platform_lag_ms) }}>{fmtMs(r.platform_lag_ms)}</strong></span>
        <span>Broker: <strong style={{ color: lagColor(r.broker_lag_median_ms) }}>{fmtMs(r.broker_lag_median_ms)}</strong></span>
      </div>
    </div>
  );
}

function SubscriberDetail({ subscribers }: { subscribers: SubscriberRow[] }) {
  if (subscribers.length === 0) {
    return <div className="text-sm py-2" style={{ color: "var(--muted)" }}>
      No subscribers were targeted (none followed with copy enabled at the time).
    </div>;
  }
  const cols = ["Subscriber", "Status", "Qty", "Filled", "Created at", "Submitted at", "Platform", "Broker", "Total", "Broker order id", "Reject reason"];
  return (
    <div className="space-y-2">
      <div className="text-[11px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>
        Per-subscriber timeline ({subscribers.length} target{subscribers.length === 1 ? "" : "s"})
      </div>
      <div className="overflow-auto">
        <table className="min-w-full text-xs">
          <thead><tr>
            {cols.map(h => (
              <th key={h} className="text-left px-3 py-2 font-medium whitespace-nowrap" style={{ color: "var(--muted)" }}>{h}</th>
            ))}
          </tr></thead>
          <tbody>
            {subscribers.map(s => (
              <tr key={s.child_order_id} className="border-t" style={{ borderColor: "var(--border)" }}>
                <td className="px-3 py-2 whitespace-nowrap">{s.display_name || s.email || s.user_id.slice(0, 8)}</td>
                <td className="px-3 py-2">{statusBadge(s.status)}</td>
                <td className="px-3 py-2 num">{s.quantity}</td>
                <td className="px-3 py-2 num">{s.filled_quantity}</td>
                <td className="px-3 py-2 num whitespace-nowrap" style={{ color: "var(--muted)" }}>{fmtTime(s.child_created_at)}</td>
                <td className="px-3 py-2 num whitespace-nowrap" style={{ color: "var(--muted)" }}>{fmtTime(s.child_submitted_at)}</td>
                <td className="px-3 py-2 num font-medium whitespace-nowrap" style={{ color: lagColor(s.platform_ms) }}>{fmtMs(s.platform_ms)}</td>
                <td className="px-3 py-2 num font-medium whitespace-nowrap" style={{ color: lagColor(s.broker_ms) }}>{fmtMs(s.broker_ms)}</td>
                <td className="px-3 py-2 num font-semibold whitespace-nowrap" style={{ color: lagColor(s.total_ms) }}>{fmtMs(s.total_ms)}</td>
                <td className="px-3 py-2 whitespace-nowrap" style={{ color: "var(--muted)" }}>
                  {s.broker_order_id ? <code className="text-[10px]">{s.broker_order_id.slice(0, 8)}…</code> : "—"}
                </td>
                <td className="px-3 py-2" style={{ color: "var(--bad)" }}>{s.reject_reason || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SummaryCard({ label, value, color, hint }: { label: string; value: string; color?: string; hint?: string }) {
  return (
    <div className="p-4 rounded border" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
      <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>{label}</div>
      <div className="text-2xl font-semibold mt-1 num" style={{ color: color || "var(--text)" }}>{value}</div>
      {hint && <div className="text-[10px] mt-1" style={{ color: "var(--muted)" }}>{hint}</div>}
    </div>
  );
}
