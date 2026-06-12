"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { brokerLabel } from "@/lib/format";
import type { BrokerAccount, Role, User } from "@/lib/types";

function statusColor(s: BrokerAccount["connection_status"]): string {
  return s === "connected" ? "var(--good)" : s === "error" ? "var(--bad)" : "var(--muted)";
}

function fmtMoney(amount: string | null, currency: string | null): string {
  if (amount === null) return "—";
  const v = Number(amount);
  if (!Number.isFinite(v)) return "—";
  try {
    return v.toLocaleString(undefined, {
      style: "currency",
      currency: currency || "USD",
      maximumFractionDigits: 2,
    });
  } catch {
    return `${v.toFixed(2)} ${currency ?? ""}`.trim();
  }
}

function fmtRelative(iso: string | null): string {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return "just now";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}

export default function BrokersPage() {
  const [accounts, setAccounts] = useState<BrokerAccount[]>([]);
  // Subscribers see a broker-agnostic UI ("Brokerage"); traders/admins see
  // the real broker names. Role drives the white-label.
  const [role, setRole] = useState<Role | null>(null);
  const isSubscriber = role === "subscriber";
  // Which form to render (only one broker form at a time).
  const [brokerType, setBrokerType] = useState<"alpaca" | "ibkr">("alpaca");

  // Alpaca form
  const [label, setLabel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [paper, setPaper] = useState(true);

  // IBKR form
  const [ibkrLabel, setIbkrLabel] = useState("");
  const [ibkrAccessToken, setIbkrAccessToken] = useState("");
  const [ibkrAccessTokenSecret, setIbkrAccessTokenSecret] = useState("");
  const [ibkrAccountId, setIbkrAccountId] = useState("");
  const [ibkrPaper, setIbkrPaper] = useState(false);

  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({});
  const [syncing, setSyncing] = useState<null | "open" | "filled">(null);

  async function load() {
    setAccounts(await api<BrokerAccount[]>("/api/brokers"));
  }
  useEffect(() => {
    load();
    api<User>("/api/auth/me").then(u => setRole(u.role)).catch(() => {});
  }, []);

  async function connect(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/api/brokers", {
        method: "POST",
        body: JSON.stringify({
          broker: "alpaca",
          label: label || (paper ? "Alpaca Paper" : "Alpaca"),
          alpaca: { api_key: apiKey, api_secret: apiSecret, paper },
        }),
      });
      setLabel(""); setApiKey(""); setApiSecret("");
      notify.success("Broker connected — balance fetched");
      await load();
    } catch (e) {
      notify.fromError(e, "Broker connect failed");
    } finally {
      setBusy(false);
    }
  }

  async function connectIbkr(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/api/brokers", {
        method: "POST",
        body: JSON.stringify({
          broker: "ibkr",
          label: ibkrLabel || (ibkrPaper ? "IBKR Paper" : "IBKR"),
          ibkr: {
            access_token: ibkrAccessToken,
            access_token_secret: ibkrAccessTokenSecret,
            account_id: ibkrAccountId,
            paper: ibkrPaper,
          },
        }),
      });
      setIbkrLabel(""); setIbkrAccessToken(""); setIbkrAccessTokenSecret(""); setIbkrAccountId("");
      notify.success("IBKR connected — balance fetched");
      await load();
    } catch (e) {
      notify.fromError(e, "IBKR connect failed");
    } finally {
      setBusy(false);
    }
  }

  async function refreshBalance(id: string) {
    setRefreshing(p => ({ ...p, [id]: true }));
    try {
      const updated = await api<BrokerAccount>(`/api/brokers/${id}/refresh-balance`, { method: "POST" });
      setAccounts(cur => cur.map(a => (a.id === id ? updated : a)));
      notify.success("Balance refreshed");
    } catch (e) {
      notify.fromError(e, "Balance refresh failed");
    } finally {
      setRefreshing(p => ({ ...p, [id]: false }));
    }
  }

  async function bringOrders(scope: "open" | "filled") {
    setSyncing(scope);
    try {
      const res = await api<{ new_open_orders: number; fills_added: number; orders_added: number; errors: string[] }>(
        `/api/brokers/sync-orders?scope=${scope}`,
        { method: "POST" },
      );
      if (scope === "open") {
        notify[res.new_open_orders > 0 ? "success" : "info"](
          res.new_open_orders > 0
            ? `Pulled ${res.new_open_orders} order${res.new_open_orders === 1 ? "" : "s"} — see Order History`
            : "No new orders found at your broker",
        );
      } else {
        const n = res.orders_added + res.fills_added;
        notify[n > 0 ? "success" : "info"](
          n > 0
            ? `Synced ${res.fills_added} fill${res.fills_added === 1 ? "" : "s"}${res.orders_added ? ` + ${res.orders_added} filled order(s)` : ""} — see Order History`
            : "No new fills found",
        );
      }
      if (res.errors?.length) notify.warn(`Some accounts had issues: ${res.errors[0]}`);
    } catch (e) {
      notify.fromError(e, "Could not bring orders");
    } finally {
      setSyncing(null);
    }
  }

  async function remove(id: string) {
    if (!confirm("Disconnect this brokerage?")) return;
    try {
      await api(`/api/brokers/${id}`, { method: "DELETE" });
      notify.success("Broker disconnected");
    } catch (e) {
      notify.fromError(e, "Disconnect failed");
    }
    load();
  }

  return (
    <div className="space-y-8 max-w-4xl">
      <h1 className="text-2xl font-semibold">Broker connections</h1>

      <p className="text-sm" style={{ color: "var(--muted)" }}>
        {isSubscriber ? (
          <>
            Connect your brokerage to start mirroring trades. Your keys never
            leave the server — they&apos;re encrypted at rest with Fernet (AES-128).
          </>
        ) : (
          <>
            Currently supported: <strong>Alpaca</strong> (production-ready) and{" "}
            <strong>IBKR</strong> (pending validation against a real IBKR account — your
            connect attempt will return 501 until your operator finishes IBKR&apos;s
            third-party onboarding and sets the consumer-key env vars on the backend).
            Keys never leave the server — they&apos;re encrypted at rest with Fernet (AES-128).
          </>
        )}
      </p>

      <section className="space-y-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2 className="text-sm uppercase tracking-wider" style={{ color: "var(--muted)" }}>Your connections</h2>
          {accounts.length > 0 && (
            <div className="flex items-center gap-2">
              <button
                onClick={() => bringOrders("open")}
                disabled={syncing !== null}
                title="Pull orders placed directly at your broker into the app"
                className="btn-ghost px-3 py-1.5 text-sm inline-flex items-center gap-1.5"
              >
                <span>Bring Open Orders</span>
                {syncing === "open" && <Spinner />}
              </button>
              <button
                onClick={() => bringOrders("filled")}
                disabled={syncing !== null}
                title="Sync filled trades from your broker into the app"
                className="btn-ghost px-3 py-1.5 text-sm inline-flex items-center gap-1.5"
              >
                <span>Bring Filled Orders</span>
                {syncing === "filled" && <Spinner />}
              </button>
            </div>
          )}
        </div>
        {accounts.length === 0 && <p style={{ color: "var(--muted)" }}>No brokers connected yet — fill in the form below to add one.</p>}
        <div className="space-y-2">
          {accounts.map(a => (
            <div key={a.id} className="card p-4">
              <div className="flex items-start justify-between">
                <div>
                  <div className="font-medium">
                    {a.label}
                    <span className="text-xs uppercase ml-2 tracking-wider" style={{ color: "var(--muted)" }}>
                      {brokerLabel(a.broker, role, a.brokerage_name)}{a.is_paper ? " · paper" : ""}{a.supports_fractional ? " · fractional" : ""}
                    </span>
                  </div>
                  <div className="text-xs mt-1" style={{ color: statusColor(a.connection_status) }}>
                    ● {a.connection_status}
                  </div>
                  {a.broker_account_number && (
                    <div className="text-xs mt-1" style={{ color: "var(--muted)" }}>
                      account: {a.broker_account_number}
                    </div>
                  )}
                  {a.last_error && (
                    <div className="text-xs mt-1" style={{ color: "var(--bad)" }}>{a.last_error}</div>
                  )}
                </div>
                <button
                  onClick={() => remove(a.id)}
                  className="btn-ghost px-3 py-1 text-sm"
                  style={{ color: "var(--bad)", borderColor: "rgba(255,107,107,0.4)" }}
                >
                  Disconnect
                </button>
              </div>

              {/* Balance row */}
              <div className="mt-3 pt-3 border-t flex items-end justify-between" style={{ borderColor: "var(--border)" }}>
                <div className="grid grid-cols-3 gap-6 flex-1">
                  <div>
                    <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Cash</div>
                    <div className="text-sm font-medium mt-0.5 num">{fmtMoney(a.cash, a.currency)}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Buying power</div>
                    <div className="text-sm font-medium mt-0.5 num">{fmtMoney(a.buying_power, a.currency)}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Total equity</div>
                    <div className="text-sm font-medium mt-0.5 num">{fmtMoney(a.total_equity, a.currency)}</div>
                  </div>
                </div>
                <div className="flex items-center gap-2 ml-4">
                  <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                    updated {fmtRelative(a.balance_updated_at)}
                  </span>
                  <button
                    onClick={() => refreshBalance(a.id)}
                    disabled={refreshing[a.id]}
                    className="btn-ghost px-2 py-1 text-sm inline-flex items-center gap-1.5"
                    title="Refresh balance"
                  >
                    <span>↻</span>
                    {refreshing[a.id] && <Spinner />}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* Broker selector — shown above whichever connect form is active. */}
      <section className="space-y-3">
        <div className="flex gap-2">
          {/* Subscribers get a broker-agnostic single option; traders/admins
              see the real broker choices. IBKR is hidden from subscribers. */}
          {(isSubscriber ? (["alpaca"] as const) : (["alpaca", "ibkr"] as const)).map((b) => {
            const active = brokerType === b;
            const alreadyConnected = accounts.some(a => a.broker === b);
            const display = brokerLabel(b, role);
            return (
              <button
                key={b}
                type="button"
                onClick={() => setBrokerType(b)}
                disabled={alreadyConnected}
                title={alreadyConnected ? `${display} is already connected` : undefined}
                className="px-4 py-2 text-sm font-medium rounded transition-colors"
                style={{
                  border: `1px solid ${active ? "rgba(10,115,168,0.4)" : "var(--border)"}`,
                  background: active ? "rgba(10,115,168,0.16)" : "transparent",
                  color: alreadyConnected ? "var(--muted)" : active ? "var(--accent)" : "var(--text-2)",
                  opacity: alreadyConnected ? 0.5 : 1,
                  cursor: alreadyConnected ? "not-allowed" : "pointer",
                }}
              >
                {display}
                {alreadyConnected && " ✓"}
              </button>
            );
          })}
        </div>
      </section>

      {brokerType === "alpaca" && !accounts.some(a => a.broker === "alpaca") && (
      <section className="card p-5 space-y-4 max-w-lg">
        <h2 className="font-semibold">{isSubscriber ? "Connect your brokerage" : "Connect an Alpaca account"}</h2>
        <p className="text-xs" style={{ color: "var(--muted)" }}>
          From <a href="https://app.alpaca.markets" target="_blank" rel="noreferrer" className="underline" style={{ color: "var(--accent)" }}>app.alpaca.markets</a>:
          {" "}select Paper Trading → click your name → API Keys → Generate.
        </p>
        <form onSubmit={connect} className="space-y-3">
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Label (optional)</label>
            <input className="w-full p-2" placeholder="Alpaca Paper" value={label} onChange={e => setLabel(e.target.value)} />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>API key ID</label>
            <input className="w-full p-2 font-mono text-sm" placeholder="PKxxxxxxxxxxxxxxxxxx" value={apiKey} onChange={e => setApiKey(e.target.value)} required />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Secret key</label>
            <input className="w-full p-2 font-mono text-sm" placeholder="(only shown once at generation)" type="password" value={apiSecret} onChange={e => setApiSecret(e.target.value)} required />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={paper} onChange={e => setPaper(e.target.checked)} />
            <span>Paper-trading account (recommended for testing)</span>
          </label>
          <button disabled={busy} className="btn-primary px-4 py-2 text-sm inline-flex items-center gap-2">
            <span>Connect</span>
            {busy && <Spinner />}
          </button>
        </form>
      </section>
      )}

      {brokerType === "ibkr" && !accounts.some(a => a.broker === "ibkr") && (
      <section className="card p-5 space-y-4 max-w-lg">
        <h2 className="font-semibold">Connect an Interactive Brokers account</h2>
        <p className="text-xs" style={{ color: "var(--muted)" }}>
          IBKR uses OAuth 1.0a. Your operator (the Option Haven admin) registers the
          app with IBKR via{" "}
          <a href="https://www.interactivebrokers.com/campus/ibkr-api-page/oauth-1-0a-extended/" target="_blank" rel="noreferrer" className="underline" style={{ color: "var(--accent)" }}>IBKR's third-party API onboarding</a>.
          You authorize the app against your account and paste the resulting
          access token + secret here. Your account ID is the one starting with
          a letter (e.g. <code>U1234567</code>).
        </p>
        <div
          className="text-xs px-3 py-2 rounded border"
          style={{ borderColor: "var(--border)", background: "rgba(217, 119, 6, 0.08)", color: "var(--text-2)" }}
        >
          <strong>Pending validation:</strong> the adapter shell is in place but
          hasn't been tested end-to-end against a real IBKR account yet. Connect
          attempts will succeed only after the backend operator has set the
          IBKR consumer-key + signing-PEM env vars (see
          <code className="ml-1">doc/DEPLOY.md</code>).
        </div>
        <form onSubmit={connectIbkr} className="space-y-3">
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Label (optional)</label>
            <input className="w-full p-2" placeholder="IBKR" value={ibkrLabel} onChange={e => setIbkrLabel(e.target.value)} />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Account ID</label>
            <input className="w-full p-2 font-mono text-sm" placeholder="U1234567" value={ibkrAccountId} onChange={e => setIbkrAccountId(e.target.value)} required />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Access token</label>
            <input className="w-full p-2 font-mono text-sm" placeholder="OAuth access token" value={ibkrAccessToken} onChange={e => setIbkrAccessToken(e.target.value)} required />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Access token secret</label>
            <input className="w-full p-2 font-mono text-sm" type="password" placeholder="OAuth access token secret" value={ibkrAccessTokenSecret} onChange={e => setIbkrAccessTokenSecret(e.target.value)} required />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={ibkrPaper} onChange={e => setIbkrPaper(e.target.checked)} />
            <span>Paper-trading account (IBKR paper logins start with <code>DU</code>)</span>
          </label>
          <button disabled={busy} className="btn-primary px-4 py-2 text-sm inline-flex items-center gap-2">
            <span>Connect</span>
            {busy && <Spinner />}
          </button>
        </form>
      </section>
      )}
    </div>
  );
}
