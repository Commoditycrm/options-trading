"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
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
  subscriber_lag_ms: number | null;        // parent.detected_at → child.submitted_at
  reject_reason: string | null;
}

interface FanoutRow {
  order_id: string;
  symbol: string;
  side: string;
  quantity: string;
  instrument_type: string;
  submitted_at: string | null;
  detected_at: string | null;
  fanout_completed_at: string | null;
  detection_lag_ms: number | null;
  fanout_duration_ms: number | null;
  total_ms: number | null;
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

function lagColor(ms: number | null | undefined): string {
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

function statusBadge(status: string) {
  const map: Record<string, { bg: string; color: string }> = {
    filled:            { bg: "var(--good-soft)",                color: "var(--good)"  },
    submitted:         { bg: "rgba(10,115,168,0.10)",           color: "var(--accent)" },
    accepted:          { bg: "rgba(10,115,168,0.10)",           color: "var(--accent)" },
    partially_filled:  { bg: "rgba(10,115,168,0.10)",           color: "var(--accent)" },
    pending:           { bg: "rgba(255,255,255,0.04)",          color: "var(--muted)" },
    rejected:          { bg: "var(--bad-soft)",                 color: "var(--bad)"   },
    canceled:          { bg: "rgba(255,255,255,0.04)",          color: "var(--muted)" },
    expired:           { bg: "rgba(255,255,255,0.04)",          color: "var(--muted)" },
  };
  const s = map[status] || { bg: "rgba(255,255,255,0.04)", color: "var(--muted)" };
  return (
    <span
      className="text-[11px] uppercase tracking-wider px-2 py-[3px] rounded whitespace-nowrap font-medium"
      style={{ background: s.bg, color: s.color }}
    >
      {status.replace("_", " ")}
    </span>
  );
}

export default function FanoutPerformancePage() {
  const [rows, setRows] = useState<FanoutRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

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
      setTimeout(load, 1000);
    }
  });

  const stats = useMemo(() => {
    const withFanout = rows.filter(r => r.fanout_duration_ms !== null);
    if (withFanout.length === 0) return null;
    const fanouts = withFanout.map(r => r.fanout_duration_ms!);
    const totals = rows.filter(r => r.total_ms !== null).map(r => r.total_ms!);
    return {
      count: rows.length,
      avgFanout: Math.round(fanouts.reduce((a, b) => a + b, 0) / fanouts.length),
      maxFanout: Math.max(...fanouts),
      avgTotal: totals.length ? Math.round(totals.reduce((a, b) => a + b, 0) / totals.length) : null,
      maxTotal: totals.length ? Math.max(...totals) : null,
    };
  }, [rows]);

  function toggle(id: string) {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="flex flex-col h-full max-w-7xl space-y-4">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold">Fanout Performance</h1>
        <p className="text-sm" style={{ color: "var(--muted)" }}>
          Latency breakdown for your most recent trades that fanned out to subscribers.
          Click any row to see per-subscriber timing. Auto-refreshes every 5 seconds
          and on every new trade event.
        </p>
      </div>

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

      <div
        className="flex-1 min-h-0 overflow-auto rounded border"
        style={{ borderColor: "var(--border)" }}
      >
        <table className="min-w-full text-sm">
          <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
            <tr>
              <th className="w-8" />
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
                <td colSpan={11} className="px-3 py-8 text-center" style={{ color: "var(--muted)" }}>
                  <span className="inline-flex items-center gap-2">
                    <Spinner />
                    <span>Loading recent fanouts…</span>
                  </span>
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={11} className="px-3 py-6 text-center" style={{ color: "var(--muted)" }}>
                  No fanouts yet. Place a trade in Alpaca (or via Trade Panel) and it'll show up here within a couple seconds.
                </td>
              </tr>
            )}
            {rows.map(r => {
              const isOpen = expanded.has(r.order_id);
              return (
                <Fragment key={r.order_id}>
                  <tr
                    className="border-t cursor-pointer transition-colors hover:bg-opacity-50"
                    style={{ borderColor: "var(--border)" }}
                    onClick={() => toggle(r.order_id)}
                  >
                    <td className="px-2 py-3 text-center" style={{ color: "var(--muted)" }}>
                      {isOpen ? "▾" : "▸"}
                    </td>
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
                  {isOpen && (
                    <tr style={{ background: "var(--bg)" }}>
                      <td colSpan={11} className="px-4 pt-2 pb-4">
                        <SubscriberDetail
                          parentDetectedAt={r.detected_at}
                          subscribers={r.subscribers}
                        />
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
        <strong>Detection lag</strong> = time between Alpaca accepting your order and our backend seeing it (limited by the 2-second poll interval today, ~100ms once the WebSocket is restored).
        {" "}<strong>Fanout duration</strong> = time from our detection to the last subscriber's order being accepted at their broker (parallel via Redis Streams + worker pool).
        {" "}<strong>Total</strong> = end-to-end (Alpaca-accept → last subscriber submitted).
        {" "}<strong>Subscriber lag</strong> (per row when expanded) = our detection → that subscriber's broker accept.
      </p>
    </div>
  );
}

function SubscriberDetail({
  parentDetectedAt,
  subscribers,
}: {
  parentDetectedAt: string | null;
  subscribers: SubscriberRow[];
}) {
  if (subscribers.length === 0) {
    return (
      <div className="text-sm py-2" style={{ color: "var(--muted)" }}>
        No subscribers were targeted (none followed the trader with copy enabled at the time).
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <div className="text-[11px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>
        Per-subscriber timeline ({subscribers.length} target{subscribers.length === 1 ? "" : "s"})
      </div>
      <table className="min-w-full text-xs">
        <thead>
          <tr>
            {[
              "Subscriber", "Status", "Qty", "Filled qty",
              "Created at", "Submitted at", "Subscriber lag",
              "Broker order id", "Reject reason",
            ].map(h => (
              <th key={h}
                  className="text-left px-3 py-2 font-medium whitespace-nowrap"
                  style={{ color: "var(--muted)" }}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {subscribers.map(s => (
            <tr key={s.child_order_id} className="border-t" style={{ borderColor: "var(--border)" }}>
              <td className="px-3 py-2 whitespace-nowrap">
                {s.display_name || s.email || s.user_id.slice(0, 8)}
              </td>
              <td className="px-3 py-2">{statusBadge(s.status)}</td>
              <td className="px-3 py-2 num">{s.quantity}</td>
              <td className="px-3 py-2 num">{s.filled_quantity}</td>
              <td className="px-3 py-2 num whitespace-nowrap" style={{ color: "var(--muted)" }}>
                {fmtTime(s.child_created_at)}
              </td>
              <td className="px-3 py-2 num whitespace-nowrap" style={{ color: "var(--muted)" }}>
                {fmtTime(s.child_submitted_at)}
              </td>
              <td className="px-3 py-2 num font-medium whitespace-nowrap"
                  style={{ color: lagColor(s.subscriber_lag_ms) }}>
                {fmtMs(s.subscriber_lag_ms)}
              </td>
              <td className="px-3 py-2 whitespace-nowrap" style={{ color: "var(--muted)" }}>
                {s.broker_order_id ? (
                  <code className="text-[10px]">{s.broker_order_id.slice(0, 8)}…</code>
                ) : "—"}
              </td>
              <td className="px-3 py-2" style={{ color: "var(--bad)" }}>
                {s.reject_reason || ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
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
