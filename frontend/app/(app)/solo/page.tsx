"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { ConfirmModal } from "@/components/ConfirmModal";

type Mode = "market" | "bid" | "ask";

interface Position {
  broker_account_id: string;
  broker_symbol: string;
  symbol: string;
  instrument_type: string;
  quantity: string;          // signed
  avg_entry_price: string | null;
  current_price: string | null;
  unrealized_pnl: string | null;
}

interface SimItem {
  item_id: string;
  order_id: string | null;
  order_status: string | null;
  filled_avg_price: string | null;
  reject_reason: string | null;
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
  reentered_at?: string | null;
  items: SimItem[];
  any_rejected?: boolean;
  quotes_available?: boolean;
}

const fmt = (v: string | null | undefined) =>
  v == null || v === "" ? "—" : Number(v).toLocaleString(undefined, { style: "currency", currency: "USD" });

const MODES: { mode: Mode; label: string }[] = [
  { mode: "bid", label: "Bid" },
  { mode: "market", label: "Market" },
  { mode: "ask", label: "Ask" },
];

const posKey = (p: { broker_account_id: string; broker_symbol: string }) =>
  `${p.broker_account_id}:${p.broker_symbol}`;

function statusBadge(status: string | null) {
  if (!status) return <span style={{ color: "var(--muted)" }}>—</span>;
  const map: Record<string, { bg: string; color: string }> = {
    filled: { bg: "var(--good-soft)", color: "var(--good)" },
    submitted: { bg: "rgba(10,115,168,0.10)", color: "var(--accent)" },
    accepted: { bg: "rgba(10,115,168,0.10)", color: "var(--accent)" },
    partially_filled: { bg: "rgba(10,115,168,0.10)", color: "var(--accent)" },
    pending: { bg: "rgba(255,255,255,0.04)", color: "var(--muted)" },
    rejected: { bg: "var(--bad-soft)", color: "var(--bad)" },
    canceled: { bg: "rgba(255,255,255,0.04)", color: "var(--muted)" },
    expired: { bg: "rgba(255,255,255,0.04)", color: "var(--muted)" },
  };
  const s = map[status] || { bg: "rgba(255,255,255,0.04)", color: "var(--muted)" };
  return (
    <span className="text-[11px] uppercase tracking-wider px-2 py-[3px] rounded whitespace-nowrap font-medium"
      style={{ background: s.bg, color: s.color }}>
      {status.replace("_", " ")}
    </span>
  );
}

export default function SoloPage() {
  const [positions, setPositions] = useState<Position[] | null>(null);
  const [posBusy, setPosBusy] = useState(false);
  // Position keys the trader has UNCHECKED (excluded from Exit All). Default =
  // everything checked, so the common one-click flow stays unchanged.
  const [excluded, setExcluded] = useState<Set<string>>(new Set());

  const [sim, setSim] = useState<Simulation | null>(null);
  // Re-enter selection: snapshot item_ids the trader has UNCHECKED.
  const [reExcluded, setReExcluded] = useState<Set<string>>(new Set());

  const [exitBusy, setExitBusy] = useState<Mode | null>(null);
  const [reenterBusy, setReenterBusy] = useState<Mode | null>(null);
  const [confirm, setConfirm] = useState<{ action: "exit" | "reenter"; mode: Mode } | null>(null);

  // Auto re-enter % (solo_reenter_pct). "" = off.
  const [reenterPct, setReenterPct] = useState("");
  const [savedPct, setSavedPct] = useState("");
  const [savingPct, setSavingPct] = useState(false);

  const loadSettings = useCallback(async () => {
    try {
      const s = await api<{ solo_reenter_pct: string | null }>("/api/settings/trader");
      const v = s.solo_reenter_pct == null ? "" : String(Number(s.solo_reenter_pct));
      setReenterPct(v); setSavedPct(v);
    } catch { /* leave blank */ }
  }, []);

  async function saveReenterPct() {
    setSavingPct(true);
    try {
      const pct = reenterPct.trim() === "" ? null : Number(reenterPct);
      if (pct !== null && (!isFinite(pct) || pct <= 0 || pct > 95)) {
        notify.warn("Enter a % between 0 and 95, or leave blank to turn off."); return;
      }
      await api("/api/settings/trader/solo-reenter-pct", { method: "PATCH", body: JSON.stringify({ pct }) });
      setSavedPct(reenterPct.trim() === "" ? "" : String(pct));
      notify.success(pct == null ? "Auto re-enter turned off." : `Auto re-enter set to ${pct}%.`);
    } catch (e) { notify.fromError(e, "Could not save auto re-enter"); }
    finally { setSavingPct(false); }
  }

  const loadPositions = useCallback(async (quiet = false) => {
    if (!quiet) setPosBusy(true);
    try {
      const ps = (await api<Position[]>("/api/positions")).filter(p => Number(p.quantity) !== 0);
      setPositions(ps);
      // Drop excludes that no longer correspond to a held position.
      setExcluded(prev => {
        const valid = new Set(ps.map(posKey));
        return new Set([...prev].filter(k => valid.has(k)));
      });
    } catch { /* keep last positions on a transient error */ }
    finally { if (!quiet) setPosBusy(false); }
  }, []);

  const loadSim = useCallback(async () => {
    try { setSim(await api<Simulation>("/api/solo/simulation")); }
    catch { /* keep last state */ }
  }, []);

  useEffect(() => {
    loadPositions();
    loadSim();
    loadSettings();
    // Keep BOTH tables live: refresh open positions (quietly) alongside the
    // simulation so a position drops out of the top table once its closing
    // order actually fills — otherwise it lingers and looks like it's "in both
    // tables" after Exit All.
    const t = setInterval(() => { loadSim(); loadPositions(true); }, 5000);
    return () => clearInterval(t);
  }, [loadPositions, loadSim, loadSettings]);

  const selectedPositions = useMemo(
    () => (positions ?? []).filter(p => !excluded.has(posKey(p))),
    [positions, excluded],
  );
  const allChecked = positions != null && excluded.size === 0;

  // Total unrealized P&L — all positions vs the checked subset — so the trader
  // can see what exiting will realize before clicking.
  const sumUpnl = (ps: Position[]) =>
    ps.reduce((a, p) => a + (p.unrealized_pnl == null ? 0 : Number(p.unrealized_pnl)), 0);
  const totalUpnl = useMemo(() => sumUpnl(positions ?? []), [positions]);
  const selectedUpnl = useMemo(() => sumUpnl(selectedPositions), [selectedPositions]);

  async function exitAll(mode: Mode) {
    setExitBusy(mode);
    try {
      // If everything is checked, send no selection → backend exits ALL and
      // cancels open orders first (identical to the original one-click). If a
      // subset is checked, send exactly those (and leave the rest untouched).
      const body = allChecked
        ? undefined
        : JSON.stringify({
            selections: selectedPositions.map(p => ({
              broker_account_id: p.broker_account_id, broker_symbol: p.broker_symbol,
            })),
          });
      const res = await api<{ closed_count: number; failed_count: number }>(
        `/api/solo/exit-all?mode=${mode}`, { method: "POST", body });
      notify[res.closed_count > 0 ? "success" : "info"](
        res.closed_count > 0
          ? `Exited ${res.closed_count} position(s) @ ${mode}${res.failed_count ? ` — ${res.failed_count} failed` : ""}`
          : "No positions exited.");
      setConfirm(null);
      loadPositions();
      loadSim();
    } catch (e) { notify.fromError(e, "Exit all failed"); }
    finally { setExitBusy(null); }
  }

  async function reenterAll(mode: Mode) {
    setReenterBusy(mode);
    try {
      const wanted = (sim?.items ?? []).filter(it => !reExcluded.has(it.item_id));
      const allItems = (sim?.items.length ?? 0) === wanted.length;
      const body = allItems ? undefined : JSON.stringify({ item_ids: wanted.map(it => it.item_id) });
      const res = await api<{ placed_count: number; failed_count: number }>(
        `/api/solo/reenter-all?mode=${mode}`, { method: "POST", body });
      notify[res.placed_count > 0 ? "success" : "info"](
        `Re-entered ${res.placed_count} position(s) @ ${mode}${res.failed_count ? ` — ${res.failed_count} failed` : ""}`);
      setConfirm(null);
      setReExcluded(new Set());
      loadPositions();
      loadSim();
    } catch (e) { notify.fromError(e, "Re-enter failed"); }
    finally { setReenterBusy(null); }
  }

  const hasSnapshot = !!sim?.snapshot_id && !sim?.reentered_at && (sim?.items.length ?? 0) > 0;
  const busy = exitBusy !== null || reenterBusy !== null;
  const exitCount = selectedPositions.length;

  function toggle(key: string) {
    setExcluded(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }
  function toggleAll() {
    setExcluded(prev => (prev.size === 0 ? new Set((positions ?? []).map(posKey)) : new Set()));
  }
  function toggleRe(id: string) {
    setReExcluded(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  return (
    <div className="space-y-5 max-w-5xl">
      <h1 className="text-2xl font-semibold">Solo trader</h1>

      {/* Exit controls + per-position checkboxes */}
      <section className="p-4 rounded border space-y-3" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div>
            <h2 className="font-medium">Exit positions</h2>
            <p className="text-sm" style={{ color: "var(--muted)" }}>
              Every position is checked by default — one click exits them all. Uncheck any you
              want to keep. Bid/Ask place limit orders at the live quote (fall back to market).
            </p>
          </div>
          <button onClick={() => loadPositions()} disabled={posBusy}
            className="text-sm px-2 py-1 rounded border inline-flex items-center gap-2"
            style={{ borderColor: "var(--border)", color: "var(--muted)" }}>
            {posBusy ? <Spinner /> : "↻"} Refresh
          </button>
        </div>

        {positions == null ? (
          <p style={{ color: "var(--muted)" }}><span className="inline-flex items-center gap-2"><Spinner /> Loading positions…</span></p>
        ) : positions.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>No open positions.</p>
        ) : (
          <div className="overflow-x-auto rounded border" style={{ borderColor: "var(--border)" }}>
            <table className="w-full text-sm">
              <thead style={{ background: "var(--panel)" }}>
                <tr>
                  <th className="px-3 py-2 w-8">
                    <input type="checkbox" checked={allChecked} onChange={toggleAll} aria-label="Select all" />
                  </th>
                  {["Contract", "Side", "Qty", "Avg entry", "Mark", "Unrealized"].map(h => (
                    <th key={h} className="text-left px-3 py-2 font-medium" style={{ color: "var(--muted)" }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {positions.map(p => {
                  const k = posKey(p);
                  const qty = Number(p.quantity);
                  const isLong = qty > 0;
                  const upnl = p.unrealized_pnl == null ? null : Number(p.unrealized_pnl);
                  return (
                    <tr key={k} className="border-t" style={{ borderColor: "var(--border)" }}>
                      <td className="px-3 py-2">
                        <input type="checkbox" checked={!excluded.has(k)} onChange={() => toggle(k)} aria-label={`Exit ${p.broker_symbol}`} />
                      </td>
                      <td className="px-3 py-2 num">{p.broker_symbol}</td>
                      <td className="px-3 py-2" style={{ color: isLong ? "var(--good)" : "var(--bad)" }}>{isLong ? "LONG" : "SHORT"}</td>
                      <td className="px-3 py-2 num">{Math.abs(qty)}</td>
                      <td className="px-3 py-2 num">{fmt(p.avg_entry_price)}</td>
                      <td className="px-3 py-2 num">{fmt(p.current_price)}</td>
                      <td className="px-3 py-2 num" style={{ color: upnl == null ? "var(--muted)" : upnl >= 0 ? "var(--good)" : "var(--bad)" }}>
                        {upnl == null ? "—" : fmt(p.unrealized_pnl)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {positions != null && positions.length > 0 && (
          <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
            <span style={{ color: "var(--muted)" }}>
              Total unrealized P&amp;L:{" "}
              <strong className="num" style={{ color: totalUpnl >= 0 ? "var(--good)" : "var(--bad)" }}>{fmt(String(totalUpnl))}</strong>
            </span>
            <span style={{ color: "var(--muted)" }}>
              Selected ({exitCount} of {positions.length}):{" "}
              <strong className="num" style={{ color: selectedUpnl >= 0 ? "var(--good)" : "var(--bad)" }}>{fmt(String(selectedUpnl))}</strong>
            </span>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2">
          {MODES.map(m => (
            <button key={m.mode} onClick={() => setConfirm({ action: "exit", mode: m.mode })}
              disabled={busy || exitCount === 0}
              className="btn-danger-soft px-3 py-2 text-sm font-medium inline-flex items-center gap-2 disabled:opacity-50">
              <span>Exit All @ {m.label}</span>
              {exitBusy === m.mode && <Spinner />}
            </button>
          ))}
        </div>
      </section>

      {/* Auto re-enter setting */}
      <section className="p-4 rounded border space-y-2" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
        <h2 className="font-medium">Auto re-enter</h2>
        <p className="text-sm" style={{ color: "var(--muted)" }}>
          After an Exit All, automatically re-enter each position when its price moves this %
          favorably from your exit — a long buys back on a dip, a short re-shorts on a rise.
          Placed as a limit order at the trigger price. Needs live market-data; leave blank to turn off.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex items-center gap-1">
            <input
              type="number" min="0" max="95" step="0.5" inputMode="decimal"
              value={reenterPct} onChange={e => setReenterPct(e.target.value)}
              placeholder="off"
              className="w-24 p-2 rounded bg-transparent border text-sm"
              style={{ borderColor: "var(--border)", color: "var(--text)" }}
            />
            <span className="text-sm" style={{ color: "var(--muted)" }}>%</span>
          </div>
          <button onClick={saveReenterPct} disabled={savingPct || reenterPct === savedPct}
            className="px-3 py-2 rounded text-sm font-medium inline-flex items-center gap-2 disabled:opacity-50"
            style={{ background: "var(--accent)", color: "#06121f" }}>
            <span>Save</span>{savingPct && <Spinner />}
          </button>
          <span className="text-sm" style={{ color: savedPct ? "var(--good)" : "var(--muted)" }}>
            {savedPct ? `On — re-enter on a ${savedPct}% move` : "Off"}
          </span>
        </div>
      </section>

      {/* Reject banner */}
      {hasSnapshot && sim?.any_rejected && (
        <div className="p-3 rounded border text-sm" style={{ borderColor: "var(--bad)", background: "var(--bad-soft)", color: "var(--bad)" }}>
          ⚠ One or more exit orders did not go through (rejected / canceled). Check the status column below and re-exit those positions.
        </div>
      )}

      {/* Simulation + status + re-enter */}
      <section className="p-4 rounded border space-y-3" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <h2 className="font-medium">Last exit — status &amp; what-if</h2>
            <p className="text-sm" style={{ color: "var(--muted)" }}>
              Live order status of your last exit plus the &quot;P&amp;L if you had held&quot;. Refreshes every 5s.
              {sim && sim.quotes_available === false && " (Live quotes unavailable — market-data not enabled.)"}
            </p>
          </div>
          {hasSnapshot && (
            <div className="flex flex-wrap items-center gap-2">
              {MODES.map(m => (
                <button key={m.mode} onClick={() => setConfirm({ action: "reenter", mode: m.mode })} disabled={busy}
                  className="px-3 py-2 rounded text-sm font-medium inline-flex items-center gap-2"
                  style={{ background: "var(--accent)", color: "#06121f" }}>
                  <span>Re-Enter @ {m.label}</span>
                  {reenterBusy === m.mode && <Spinner />}
                </button>
              ))}
            </div>
          )}
        </div>

        {!hasSnapshot ? (
          <p style={{ color: "var(--muted)" }}>No exited positions yet — use Exit All above.</p>
        ) : (
          <div className="overflow-x-auto rounded border" style={{ borderColor: "var(--border)" }}>
            <table className="w-full text-sm">
              <thead style={{ background: "var(--panel)" }}>
                <tr>
                  <th className="px-3 py-2 w-8" title="Include in Re-Enter">✓</th>
                  {["Contract", "Side", "Qty", "Status", "Fill", "Exit", "Now (mid)", "P&L if held"].map(h => (
                    <th key={h} className="text-left px-3 py-2 font-medium" style={{ color: "var(--muted)" }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sim!.items.map(it => {
                  const pnl = it.pnl_if_held == null ? null : Number(it.pnl_if_held);
                  return (
                    <tr key={it.item_id} className="border-t" style={{ borderColor: "var(--border)" }}>
                      <td className="px-3 py-2">
                        <input type="checkbox" checked={!reExcluded.has(it.item_id)} onChange={() => toggleRe(it.item_id)} aria-label={`Re-enter ${it.symbol}`} />
                      </td>
                      <td className="px-3 py-2 num">{it.occ_symbol ?? it.symbol}</td>
                      <td className="px-3 py-2" style={{ color: it.side === "buy" ? "var(--good)" : "var(--bad)" }}>{it.side.toUpperCase()}</td>
                      <td className="px-3 py-2 num">{Number(it.quantity)}</td>
                      <td className="px-3 py-2">
                        {statusBadge(it.order_status)}
                        {it.reject_reason && <div className="text-[11px] mt-1" style={{ color: "var(--bad)" }}>{it.reject_reason}</div>}
                      </td>
                      <td className="px-3 py-2 num">{fmt(it.filled_avg_price)}</td>
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
        title={confirm?.action === "reenter" ? `Re-enter @ ${confirm.mode}?` : `Exit @ ${confirm?.mode}?`}
        message={confirm?.action === "reenter"
          ? `This rebuilds the selected position(s) from your last exit at ${confirm.mode === "market" ? "market" : `the ${confirm.mode}`} (original side + qty).`
          : `This closes ${exitCount} selected position(s) at ${confirm?.mode === "market" ? "market" : `the ${confirm?.mode}`}.`}
        confirmLabel={confirm?.action === "reenter" ? "Re-enter" : "Exit"}
        variant={confirm?.action === "reenter" ? "primary" : "danger"}
        busy={busy}
        onConfirm={() => { if (confirm?.action === "reenter") reenterAll(confirm.mode); else if (confirm) exitAll(confirm.mode); }}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}
