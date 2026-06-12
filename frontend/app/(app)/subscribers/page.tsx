"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { ConfirmModal } from "@/components/ConfirmModal";
import type { SubscriberSummary } from "@/lib/types";

export default function SubscribersPage() {
  const [rows, setRows] = useState<SubscriberSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [removing, setRemoving] = useState(false);

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

  const allSelected = rows.length > 0 && selected.size === rows.length;
  function toggleAll() {
    setSelected(allSelected ? new Set() : new Set(rows.map(r => r.user_id)));
  }
  function toggleOne(id: string) {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  async function removeSelected() {
    setRemoving(true);
    try {
      const res = await api<{ removed_count: number; skipped_count: number }>(
        "/api/subscribers/remove",
        { method: "POST", body: JSON.stringify({ subscriber_ids: [...selected] }) },
      );
      notify.success(
        `Removed ${res.removed_count} subscriber${res.removed_count === 1 ? "" : "s"}` +
        (res.skipped_count ? ` (${res.skipped_count} skipped)` : ""),
      );
      setSelected(new Set());
      setConfirmOpen(false);
      load();
    } catch (e) {
      notify.fromError(e, "Could not remove subscribers");
    } finally {
      setRemoving(false);
    }
  }

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

      {/* Bulk action bar — appears only when at least one row is selected. */}
      {selected.size > 0 && (
        <div className="flex items-center gap-3">
          <span className="text-sm" style={{ color: "var(--muted)" }}>{selected.size} selected</span>
          <button
            onClick={() => setConfirmOpen(true)}
            className="btn-danger-soft px-3 py-1.5 text-sm font-medium rounded"
          >
            Remove selected
          </button>
          <button
            onClick={() => setSelected(new Set())}
            className="px-3 py-1.5 text-sm rounded border"
            style={{ borderColor: "var(--border)", color: "var(--muted)" }}
          >
            Clear
          </button>
        </div>
      )}

      <div className="overflow-x-auto rounded border" style={{borderColor: "var(--border)"}}>
        <table className="w-full text-sm">
          <thead style={{background: "var(--panel)"}}>
            <tr>
              <th className="px-3 py-2 w-8">
                <input
                  type="checkbox"
                  aria-label="Select all subscribers"
                  checked={allSelected}
                  onChange={toggleAll}
                  disabled={rows.length === 0}
                />
              </th>
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
                <td colSpan={5} className="px-3 py-8 text-center" style={{color: "var(--muted)"}}>
                  <span className="inline-flex items-center gap-2">
                    <Spinner />
                    <span>Loading subscribers…</span>
                  </span>
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && (
              <tr><td colSpan={5} className="px-3 py-6 text-center" style={{color: "var(--muted)"}}>No subscribers yet.</td></tr>
            )}
            {rows.map(r => {
              const pnl = Number(r.realized_pnl_30d);
              const isSel = selected.has(r.user_id);
              return (
                <tr key={r.user_id} className="border-t" style={{borderColor: "var(--border)", background: isSel ? "var(--panel)" : undefined}}>
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      aria-label={`Select ${r.email}`}
                      checked={isSel}
                      onChange={() => toggleOne(r.user_id)}
                    />
                  </td>
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

      <ConfirmModal
        open={confirmOpen}
        title={`Remove ${selected.size} subscriber${selected.size === 1 ? "" : "s"}?`}
        message="They will stop following you and their copy trading will be turned off. They can follow you again later unless blocked."
        confirmLabel="Remove"
        variant="danger"
        busy={removing}
        onConfirm={removeSelected}
        onCancel={() => setConfirmOpen(false)}
      />
    </div>
  );
}
