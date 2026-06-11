"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { useBusinessName } from "@/lib/branding";

export default function ResetPasswordPage() {
  const router = useRouter();
  const businessName = useBusinessName();
  // Read the token from the URL on the client to avoid the useSearchParams()
  // Suspense requirement during static generation.
  const [token, setToken] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setToken(new URLSearchParams(window.location.search).get("token"));
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!token) { notify.error("Missing or invalid reset link."); return; }
    if (password.length < 8) { notify.warn("Password must be at least 8 characters."); return; }
    if (password !== confirm) { notify.warn("Passwords don't match."); return; }
    setLoading(true);
    try {
      const r = await api<{ message: string }>("/api/auth/reset-password", {
        method: "POST",
        body: JSON.stringify({ token, password }),
        auth: false,
      });
      notify.success(r.message);
      router.replace("/login");
    } catch (e) {
      notify.fromError(e, "Reset failed — the link may have expired");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen grid place-items-center p-6">
      <div className="card w-full max-w-md p-8 space-y-5">
        <div className="flex items-center gap-3">
          <img
            src="/brand-icon.avif"
            alt={businessName}
            width={45}
            height={45}
            style={{ width: 45, height: 45, borderRadius: 8, objectFit: "cover" }}
          />
          <div>
            <div style={{ fontWeight: 700, fontSize: 18, letterSpacing: "0.02em" }}>{businessName}</div>
            <div className="text-xs" style={{ color: "var(--muted)" }}>Choose a new password</div>
          </div>
        </div>

        <form onSubmit={submit} className="space-y-5">
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>
              New password
            </label>
            <input
              className="w-full p-2.5"
              type="password"
              autoComplete="new-password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>
              Confirm password
            </label>
            <input
              className="w-full p-2.5"
              type="password"
              autoComplete="new-password"
              placeholder="••••••••"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
            />
          </div>
          <button
            disabled={loading}
            className="btn-primary w-full py-2.5 text-sm inline-flex items-center justify-center gap-2"
          >
            <span>Update password</span>
            {loading && <Spinner />}
          </button>
        </form>

        <div className="text-center text-sm" style={{ color: "var(--muted)" }}>
          <Link href="/login" className="underline" style={{ color: "var(--accent)" }}>
            Back to sign in
          </Link>
        </div>
      </div>
    </main>
  );
}
