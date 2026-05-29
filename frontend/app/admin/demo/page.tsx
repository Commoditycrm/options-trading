"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { Spinner } from "@/components/Spinner";

interface PendingRow {
  id: string;
  subscriber_user_id: string;
  status: "queued" | "processing" | "submitted" | "failed";
  queued_at: string | null;
  picked_up_at: string | null;
  submitted_at: string | null;
  queue_to_broker_ms: number | null;
  detail: string | null;
}

interface DemoStats {
  parent_order_id: string | null;
  memory_cache: { loaded: boolean; trader_count: number; subscriber_count: number };
  worker_heartbeat: { running: boolean; seconds_since?: number; healthy?: boolean };
  rows: PendingRow[];
}

function ms(a: string | null, b: string | null): number | null {
  if (!a || !b) return null;
  return new Date(b).getTime() - new Date(a).getTime();
}

export default function QueueDemoPage() {
  const [stats, setStats] = useState<DemoStats | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const data = await api<DemoStats>("/api/admin/demo/stats");
        if (alive) setStats(data);
      } catch (e) {
        if (alive) setErr(String(e));
      }
    };
    poll();
    const t = setInterval(poll, 1000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const hotPathMs = useMemo(() => {
    if (!stats?.rows.length) return null;
    const earliestQueue = Math.min(...stats.rows.map(r => new Date(r.queued_at!).getTime()));
    const latestQueue = Math.max(...stats.rows.map(r => new Date(r.queued_at!).getTime()));
    return latestQueue - earliestQueue;
  }, [stats]);

  const serialEquiv = useMemo(() => {
    if (!stats?.rows.length) return null;
    return stats.rows.length * 23;
  }, [stats]);

  const tMin = useMemo(() => {
    if (!stats?.rows.length) return 0;
    return Math.min(...stats.rows.map(r => new Date(r.queued_at!).getTime()));
  }, [stats]);

  const tMax = useMemo(() => {
    if (!stats?.rows.length) return 1;
    const submitted = stats.rows
      .map(r => r.submitted_at ? new Date(r.submitted_at).getTime() : null)
      .filter((v): v is number => v !== null);
    if (!submitted.length) return tMin + 1000;
    return Math.max(...submitted);
  }, [stats, tMin]);

  const span = Math.max(tMax - tMin, 1);

  if (err) return <div className="p-6 text-red-400">Error: {err}</div>;
  if (!stats) return <div className="p-6"><Spinner /></div>;

  return (
    <div className="p-6 space-y-6 text-sm">
      <div>
        <h1 className="text-xl font-semibold">Queue Demo — Fanout Comparison</h1>
        <p className="text-zinc-400 mt-1">
          Latest parent order: {stats.parent_order_id ?? "(none yet)"}
        </p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card label="Memory cache" value={`${stats.memory_cache.subscriber_count} subs`}
              sub={`${stats.memory_cache.trader_count} traders`} />
        <Card label="Worker pool"
              value={stats.worker_heartbeat.healthy ? "healthy" : (stats.worker_heartbeat.running ? "stale" : "off")}
              sub={`hb ${stats.worker_heartbeat.seconds_since ?? "?"}s ago`} />
        <Card label="Queue hot path"
              value={hotPathMs !== null ? `${hotPathMs} ms` : "—"}
              sub={`${stats.rows.length} rows queued`} />
        <Card label="Serial equivalent"
              value={serialEquiv !== null ? `${serialEquiv} ms` : "—"}
              sub={`${stats.rows.length} × 23 ms`}
              accent="text-red-300" />
      </div>

      <section>
        <h2 className="font-semibold mb-2">Per-subscriber timeline</h2>
        <div className="space-y-1">
          <div className="flex text-xs text-zinc-500">
            <div className="w-48 shrink-0">subscriber</div>
            <div className="flex-1">queued → picked_up → submitted ({Math.round(span)} ms span)</div>
            <div className="w-24 text-right">q→broker</div>
            <div className="w-24 text-right">status</div>
          </div>
          {stats.rows.map(r => {
            const q = new Date(r.queued_at!).getTime() - tMin;
            const p = r.picked_up_at ? new Date(r.picked_up_at).getTime() - tMin : null;
            const s = r.submitted_at ? new Date(r.submitted_at).getTime() - tMin : null;
            const qPct = (q / span) * 100;
            const queueWaitPct = p !== null ? ((p - q) / span) * 100 : 0;
            const brokerPct = (p !== null && s !== null) ? ((s - p) / span) * 100 : 0;
            return (
              <div key={r.id} className="flex items-center">
                <div className="w-48 shrink-0 truncate text-xs font-mono">
                  {r.subscriber_user_id.slice(0, 8)}
                </div>
                <div className="flex-1 relative h-4 bg-zinc-900 rounded overflow-hidden">
                  <div className="absolute h-full bg-zinc-700"
                       style={{ left: `${qPct}%`, width: `${queueWaitPct}%` }} />
                  <div className={`absolute h-full ${r.status === "submitted" ? "bg-emerald-500" : r.status === "failed" ? "bg-red-500" : "bg-amber-500"}`}
                       style={{ left: `${qPct + queueWaitPct}%`, width: `${brokerPct}%` }} />
                </div>
                <div className="w-24 text-right text-xs font-mono">
                  {r.queue_to_broker_ms !== null ? `${r.queue_to_broker_ms} ms` : "—"}
                </div>
                <div className="w-24 text-right text-xs">{r.status}</div>
              </div>
            );
          })}
        </div>
      </section>

      <section>
        <h2 className="font-semibold mb-2">Status breakdown</h2>
        <Breakdown rows={stats.rows} />
      </section>
    </div>
  );
}

function Card({ label, value, sub, accent }:
  { label: string; value: string; sub?: string; accent?: string }) {
  return (
    <div className="rounded border border-zinc-800 p-3">
      <div className="text-xs text-zinc-500">{label}</div>
      <div className={`text-lg font-semibold ${accent ?? ""}`}>{value}</div>
      {sub && <div className="text-xs text-zinc-500">{sub}</div>}
    </div>
  );
}

function Breakdown({ rows }: { rows: PendingRow[] }) {
  const counts = rows.reduce<Record<string, number>>((acc, r) => {
    const key = r.status === "failed" && r.detail ? `failed: ${r.detail}` : r.status;
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});
  return (
    <div className="space-y-1">
      {Object.entries(counts).map(([k, v]) => (
        <div key={k} className="flex justify-between text-xs">
          <span>{k}</span>
          <span className="font-mono">{v}</span>
        </div>
      ))}
    </div>
  );
}
