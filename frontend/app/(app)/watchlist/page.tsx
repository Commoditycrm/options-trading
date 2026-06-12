"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";

export default function WatchlistPage() {
  const router = useRouter();
  const [symbols, setSymbols] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [input, setInput] = useState("");
  const [adding, setAdding] = useState(false);

  async function load() {
    try { setSymbols(await api<string[]>("/api/watchlist")); }
    catch (e) { notify.fromError(e, "Could not load watchlist"); }
    finally { setLoading(false); }
  }
  useEffect(() => { load(); }, []);

  async function add() {
    const sym = input.trim().toUpperCase();
    if (!sym) return;
    setAdding(true);
    try {
      setSymbols(await api<string[]>("/api/watchlist", { method: "POST", body: JSON.stringify({ symbol: sym }) }));
      setInput("");
    } catch (e) {
      notify.fromError(e, "Could not add symbol");
    } finally {
      setAdding(false);
    }
  }

  async function remove(sym: string) {
    try {
      setSymbols(await api<string[]>(`/api/watchlist/${encodeURIComponent(sym)}`, { method: "DELETE" }));
    } catch (e) {
      notify.fromError(e, "Could not remove symbol");
    }
  }

  return (
    <div className="space-y-5 max-w-2xl">
      <h1 className="text-2xl font-semibold">Watchlist</h1>

      <div className="flex items-center gap-2">
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") add(); }}
          placeholder="Add a ticker (e.g. AAPL)"
          className="w-48 p-2 rounded bg-transparent border uppercase"
          style={{ borderColor: "var(--border)" }}
        />
        <button
          onClick={add}
          disabled={adding || input.trim() === ""}
          className="px-4 py-2 rounded font-medium inline-flex items-center gap-2"
          style={{ background: "var(--accent)", color: "#06121f" }}
        >
          <span>Add</span>
          {adding && <Spinner />}
        </button>
      </div>

      {loading ? (
        <p style={{ color: "var(--muted)" }}>Loading…</p>
      ) : symbols.length === 0 ? (
        <p style={{ color: "var(--muted)" }}>No symbols yet — add one above.</p>
      ) : (
        <div className="rounded border divide-y" style={{ borderColor: "var(--border)" }}>
          {symbols.map(s => (
            <div key={s} className="flex items-center justify-between px-4 py-3" style={{ borderColor: "var(--border)" }}>
              <button
                onClick={() => router.push(`/trade-panel?symbol=${encodeURIComponent(s)}`)}
                className="font-medium num hover:underline"
                title="Open in the trade panel"
                style={{ color: "var(--text)" }}
              >
                {s}
              </button>
              <button
                onClick={() => remove(s)}
                className="text-sm px-2 py-1 rounded border"
                style={{ borderColor: "var(--border)", color: "var(--muted)" }}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
