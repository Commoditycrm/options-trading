"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

export default function AdminBrandingPage() {
  const [businessName, setBusinessName] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const c = await api<{ business_name: string }>("/api/admin/config");
      setBusinessName(c.business_name);
    } catch (e) {
      notify.fromError(e, "Could not load settings");
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { load(); }, []);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    const name = businessName.trim();
    if (!name) { notify.warn("Business name can't be empty"); return; }
    setSaving(true);
    try {
      await api("/api/admin/config", {
        method: "PATCH",
        body: JSON.stringify({ business_name: name }),
      });
      // Bust the cached brand name so the app picks up the change on reload.
      try { sessionStorage.removeItem("trading-app:business_name"); } catch { /* ignore */ }
      notify.success("Business name updated — reload to see it everywhere");
    } catch (e) {
      notify.fromError(e, "Save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-6 max-w-xl">
      <div>
        <h2 className="text-xl font-bold">Branding</h2>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          The business name shown across the app — browser tab title, the
          sign-in / sign-up pages, and the sidebar.
        </p>
      </div>

      <div
        className="rounded-xl p-5 space-y-4"
        style={{ background: "rgba(14,20,17,0.5)", border: "1px solid var(--border)" }}
      >
        <form onSubmit={save} className="space-y-4">
          <div className="space-y-1">
            <label className="text-xs font-medium" style={{ color: "var(--text-2)" }}>
              Business name
            </label>
            <input
              type="text"
              maxLength={120}
              value={businessName}
              onChange={e => setBusinessName(e.target.value)}
              disabled={loading}
              placeholder="The Option Haven"
              className="w-full text-sm px-3 py-2 rounded-lg"
              style={{
                background: "rgba(255,255,255,0.04)",
                border: "1px solid var(--border)",
                color: "var(--text)",
                outline: "none",
              }}
            />
          </div>
          <button
            type="submit"
            disabled={saving || loading}
            className="text-sm font-semibold px-5 py-2.5 rounded-lg"
            style={{
              background: "var(--accent)",
              color: "var(--accent-ink)",
              opacity: (saving || loading) ? 0.6 : 1,
              cursor: (saving || loading) ? "not-allowed" : "pointer",
            }}
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </form>
      </div>
    </div>
  );
}
