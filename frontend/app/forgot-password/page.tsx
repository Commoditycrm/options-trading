"use client";

import { FormEvent, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { useBusinessName } from "@/lib/branding";

export default function ForgotPasswordPage() {
  const businessName = useBusinessName();
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [sent, setSent] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    try {
      const r = await api<{ message: string }>("/api/auth/forgot-password", {
        method: "POST",
        body: JSON.stringify({ email }),
        auth: false,
      });
      setSent(true);
      notify.success(r.message);
    } catch (e) {
      notify.fromError(e, "Could not send reset link");
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
            <div className="text-xs" style={{ color: "var(--muted)" }}>Reset your password</div>
          </div>
        </div>

        {sent ? (
          <div className="text-sm" style={{ color: "var(--muted)" }}>
            If an account exists for <strong>{email}</strong>, a reset link is on its
            way. Check your inbox and follow the link to set a new password.
          </div>
        ) : (
          <form onSubmit={submit} className="space-y-5">
            <div>
              <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>
                Email
              </label>
              <input
                className="w-full p-2.5"
                type="email"
                autoComplete="email"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>
            <button
              disabled={loading}
              className="btn-primary w-full py-2.5 text-sm inline-flex items-center justify-center gap-2"
            >
              <span>Send reset link</span>
              {loading && <Spinner />}
            </button>
          </form>
        )}

        <div className="text-center text-sm" style={{ color: "var(--muted)" }}>
          <Link href="/login" className="underline" style={{ color: "var(--accent)" }}>
            Back to sign in
          </Link>
        </div>
      </div>
    </main>
  );
}
