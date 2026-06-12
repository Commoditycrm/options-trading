"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import type { SubscriberSummary } from "@/lib/types";

export default function SubscribersPage() {
  const [rows, setRows] = useState<SubscriberSummary[]>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    try { setRows(await api<SubscriberSummary[]>("/api/subscribers")); }
    catch (e) { notify.fromError(e, "Could not load subscribers"); }
    finally { setLoading(false); }
  }
  useEffect(() => { load(); }, []);

  // Totals summary (parity with the comparison: Total / Copy on / With broker).
  const total = rows.length;
  const copyOn = rows.filter(r => r.copy_enabled).length;
  const withBroker = rows.filter(r => r.broker_count > 0).length;

  return (
    <div className="space-y-4 max-w-6xl">
      {/* Totals — shown once subscribers have loaded. */}
      {!loading && total > 0 && (
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: "Subscribers", value: total },
            { label: "Copy ON", value: copyOn },
            { label: "With broker", value: withBroker },
          ].map(s => (
            <div key={s.label} className="card p-3">
              <div className="text-[10px] uppercase tracking-widest" style={{ color: "var(--muted)" }}>{s.label}</div>
              <div className="text-2xl" style={{ fontWeight: 700 }}>{s.value}</div>
            </div>
          ))}
        </div>
      )}

      <div className="overflow-x-auto rounded border" style={{borderColor: "var(--border)"}}>
        <table className="w-full text-sm">
          <thead style={{background: "var(--panel)"}}>
            <tr>
              {/* Multiplier intentionally NOT shown to the trader (per client
                  decision: the trader can't view or edit a subscriber's
                  multiplier — the subscriber controls it themselves). */}
              {["Subscriber", "Copy", "Brokers", "30d realized P&L"].map(h =>
                <th key={h} className="text-left px-3 py-2 font-medium" style={{color: "var(--muted)"}}>{h}</th>
              )}
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={4} className="px-3 py-8 text-center" style={{color: "var(--muted)"}}>
                  <span className="inline-flex items-center gap-2">
                    <Spinner />
                    <span>Loading subscribers…</span>
                  </span>
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && (
              <tr><td colSpan={4} className="px-3 py-6 text-center" style={{color: "var(--muted)"}}>No subscribers yet.</td></tr>
            )}
            {rows.map(r => {
              const pnl = Number(r.realized_pnl_30d);
              return (
                <tr key={r.user_id} className="border-t" style={{borderColor: "var(--border)"}}>
                  <td className="px-3 py-2">
                    <div>{r.display_name ?? r.email}</div>
                    <div className="text-xs" style={{color: "var(--muted)"}}>{r.email}</div>
                  </td>
                  <td className="px-3 py-2">
                    <span style={{color: r.copy_enabled ? "var(--good)" : "var(--muted)"}}>
                      {r.copy_enabled ? "ON" : "OFF"}
                    </span>
                  </td>
                  <td className="px-3 py-2">{r.broker_count}</td>
                  <td className="px-3 py-2" style={{color: pnl >= 0 ? "var(--good)" : "var(--bad)"}}>
                    {pnl.toLocaleString(undefined, { style: "currency", currency: "USD" })}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
