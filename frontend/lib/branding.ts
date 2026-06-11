"use client";

import { useEffect, useState } from "react";

export const DEFAULT_BUSINESS_NAME = "The Option Haven";
const CACHE_KEY = "trading-app:business_name";

/**
 * Client hook returning the admin-configured business name.
 *
 * Reads the public, unauthenticated GET /api/config (so it works on the
 * logged-out login/register pages). Seeds from a sessionStorage cache and
 * falls back to the default so there's no flash of empty/placeholder text
 * before the fetch resolves.
 */
export function useBusinessName(): string {
  const [name, setName] = useState<string>(() => {
    if (typeof window === "undefined") return DEFAULT_BUSINESS_NAME;
    return sessionStorage.getItem(CACHE_KEY) || DEFAULT_BUSINESS_NAME;
  });

  useEffect(() => {
    let alive = true;
    fetch("/api/config")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        const n = d?.business_name;
        if (alive && typeof n === "string" && n) {
          setName(n);
          try { sessionStorage.setItem(CACHE_KEY, n); } catch { /* ignore */ }
        }
      })
      .catch(() => { /* keep current/default */ });
    return () => { alive = false; };
  }, []);

  return name;
}
