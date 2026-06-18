"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { ConfirmModal } from "@/components/ConfirmModal";

type ExitMode = "market" | "bid" | "ask";

interface SimItem {
  symbol: string;
  occ_symbol: string | null;
  instrument_type: string;
  side: string;
  quantity: string;
  entry_price: string | null;
  exit_price: string | null;
  current_mid: string | null;
  pnl_if_held: string | null;
}
interface Simulation {
  snapshot_id: string | null;
  exit_mode?: string;
  created_at?: string;
  reentered_at?: string | null;
  items: SimItem[];
  quotes_available?: boolean;
}

const fmt = (v: string | null | undefined) =>
  v == null || v === "" ? "—" : Number(v).toLocaleString(undefined, { style: "currency", currency: "USD" });

export default function SoloPage() {
  const [sim, setSim] = useState<Simulation | null>(null);
  const [exitBusy, setExitBusy] = useState<ExitMode | null>(null);
  const [reenterBusy, setReenterBusy] = useState(false);
  const [confirm, setConfirm] = useState<ExitMode | "reenter" | null>(null);

  const loadSim = useCallback(async () => {
    try { setSim(await api<Simulation>("/api/solo/simulation")); }
    catch { /* leave last state */ }
  }, []);

  useEffect(() => {
    loadSim();
    const t = setInterval(loadSim, 5000);   // live "what-if" refresh
    return () => clearInterval(t);
  }, [loadSim]);

  async function exitAll(mode: ExitMode) {
    setExitBusy(mode);
    try {
      const res = await api<{ closed_count: number; failed_count: number }>(
        `/api/solo/exit-all?mode=${mode}`, { method: "POST" });
      notify[res.closed_count > 0 ? "success" : "info"](
        res.closed_count > 0
          ? `Exited ${res.closed_count} position(s) @ ${mode}${res.failed_count ? ` — ${res.failed_count} failed` : ""}`
          : "No open positions to exit.");
      setConfirm(null);
      loadSim();
    } catch (e) { notify.fromError(e, "Exit all failed"); }
    finally { setExitBusy(null); }
  }

  async function reenterAll() {
    setReenterBusy(true);
    try {
      const res = await api<{ placed_count: number; failed_count: number }>(
        "/api/solo/reenter-all", { method: "POST" });
      notify[res.placed_count > 0 ? "success" : "info"](
        `Re-entered ${res.placed_count} position(s)${res.failed_count ? ` — ${res.failed_count} failed` : ""}`);
      setConfirm(null);
      loadSim();
    } catch (e) { notify.fromError(e, "Re-enter failed"); }
    finally { setReenterBusy(false); }
  }

  const hasSnapshot = !!sim?.snapshot_id && !sim?.reentered_at;
  const exitBtns: { mode: ExitMode; label: string }[] = [
    { mode: "bid", label: "Exit All @ Bid" },
    { mode: "market", label: "Exit All @ Market" },
    { mode: "ask", label: "Exit All @ Ask" },
  ];

  return (
    <div className="space-y-5 max-w-5xl">
      <h1 className="text-2xl font-semibold">Solo trader</h1>

      {/* Exit controls */}
      <section className="p-4 rounded border space-y-3" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
        <h2 className="font-medium">Exit all positions</h2>
        <p className="text-sm" style={{ color: "var(--muted)" }}>
          Close every open position at once. Bid/Ask place limit orders at the live quote
          (and fall back to market if no quote is available).
        </p>
        <div className="flex flex-wrap items-center gap-2">
          {exitBtns.map(b => (
            <button key={b.mode} onClick={() => setConfirm(b.mode)} disabled={exitBusy !== null}
              className="btn-danger-soft px-3 py-2 text-sm font-medium inline-flex items-center gap-2">
              <span>{b.label}</span>
              {exitBusy === b.mode && <Spinner />}
            </button>
          ))}
        </div>
      </section>

      {/* Simulation + re-enter */}
      <section className="p-4 rounded border space-y-3" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <h2 className="font-medium">Simulation — what if you had held</h2>
            <p className="text-sm" style={{ color: "var(--muted)" }}>
              Live mark of the positions you last exited. Refreshes every 5s.
              {sim && sim.quotes_available === false && " (Live quotes unavailable — market-data not enabled.)"}
            </p>
          </div>
          {hasSnapshot && (
            <button onClick={() => setConfirm("reenter")} disabled={reenterBusy}
              className="px-4 py-2 rounded font-medium inline-flex items-center gap-2"
              style={{ background: "var(--accent)", color: "#06121f" }}>
              <span>Re-Enter All</span>
              {reenterBusy && <Spinner />}
            </button>
          )}
        </div>

        {!hasSnapshot ? (
          <p style={{ color: "var(--muted)" }}>No exited positions to simulate yet — use Exit All above.</p>
        ) : (
          <div className="overflow-x-auto rounded border" style={{ borderColor: "var(--border)" }}>
            <table className="w-full text-sm">
              <thead style={{ background: "var(--panel)" }}>
                <tr>
                  {["Contract", "Side", "Qty", "Entry", "Exit", "Now (mid)", "P&L if held"].map(h => (
                    <th key={h} className="text-left px-3 py-2 font-medium" style={{ color: "var(--muted)" }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sim!.items.map((it, i) => {
                  const pnl = it.pnl_if_held == null ? null : Number(it.pnl_if_held);
                  return (
                    <tr key={i} className="border-t" style={{ borderColor: "var(--border)" }}>
                      <td className="px-3 py-2 num">{it.occ_symbol ?? it.symbol}</td>
                      <td className="px-3 py-2" style={{ color: it.side === "buy" ? "var(--good)" : "var(--bad)" }}>{it.side.toUpperCase()}</td>
                      <td className="px-3 py-2 num">{Number(it.quantity)}</td>
                      <td className="px-3 py-2 num">{fmt(it.entry_price)}</td>
                      <td className="px-3 py-2 num">{fmt(it.exit_price)}</td>
                      <td className="px-3 py-2 num">{fmt(it.current_mid)}</td>
                      <td className="px-3 py-2 num" style={{ color: pnl == null ? "var(--muted)" : pnl >= 0 ? "var(--good)" : "var(--bad)" }}>
                        {pnl == null ? "—" : fmt(it.pnl_if_held)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <ConfirmModal
        open={confirm !== null}
        title={confirm === "reenter" ? "Re-enter all positions?" : `Exit all @ ${confirm}?`}
        message={confirm === "reenter"
          ? "This places market orders to rebuild every position from your last exit."
          : `This closes every open position at ${confirm === "market" ? "market" : `the ${confirm}`}.`}
        confirmLabel={confirm === "reenter" ? "Re-enter all" : "Exit all"}
        variant="danger"
        busy={exitBusy !== null || reenterBusy}
        onConfirm={() => { if (confirm === "reenter") reenterAll(); else if (confirm) exitAll(confirm); }}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}
