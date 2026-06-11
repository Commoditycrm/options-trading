import "./globals.css";
import "react-toastify/dist/ReactToastify.css";
import type { Metadata } from "next";
import { ToastContainer } from "react-toastify";

const DEFAULT_BUSINESS_NAME = "The Option Haven";

// Dynamic <title> from the admin-editable business name. Runs on the server,
// so it must hit the backend directly (BACKEND_URL), not the relative /api
// rewrite the browser uses. Falls back to the default on any error.
export async function generateMetadata(): Promise<Metadata> {
  let businessName = DEFAULT_BUSINESS_NAME;
  try {
    const base = process.env.BACKEND_URL || "http://localhost:8000";
    const r = await fetch(`${base}/api/config`, { cache: "no-store" });
    if (r.ok) {
      const d = await r.json();
      if (typeof d?.business_name === "string" && d.business_name) {
        businessName = d.business_name;
      }
    }
  } catch {
    /* keep default */
  }
  return {
    title: businessName,
    description: "Stock & options copy trading",
    icons: {
      icon: "/brand-icon.avif",
      shortcut: "/brand-icon.avif",
      apple: "/brand-icon.avif",
    },
  };
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body suppressHydrationWarning>
        {children}
        <ToastContainer
          position="top-right"
          autoClose={3000}
          theme="dark"
          newestOnTop
          pauseOnFocusLoss={false}
        />
      </body>
    </html>
  );
}
