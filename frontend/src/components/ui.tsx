import { ReactNode, useEffect, useRef } from "react";

export function Card({ title, right, children, className = "" }: { title?: string; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900 ${className}`}>
      {(title || right) && (
        <header className="mb-3 flex items-center gap-2">
          {title && <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h2>}
          <div className="ml-auto">{right}</div>
        </header>
      )}
      {children}
    </section>
  );
}

export function Tile({ label, value, sub, tone = "" }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "good" | "bad" | "warn" | "" }) {
  const color = tone === "good" ? "text-emerald-600 dark:text-emerald-400" : tone === "bad" ? "text-red-600 dark:text-red-400"
    : tone === "warn" ? "text-amber-600 dark:text-amber-400" : "";
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className={`mt-1 text-2xl font-semibold tabular-nums ${color}`}>{value}</p>
      {sub && <p className="mt-0.5 text-xs text-slate-500">{sub}</p>}
    </div>
  );
}

const STATUS: Record<string, [string, string]> = {
  open: ["Waiting on source", "bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-100"],
  auto_resolved: ["Auto-resolved", "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200"],
  waiting_merchant: ["Waiting on merchant", "bg-sky-100 text-sky-800 dark:bg-sky-900/50 dark:text-sky-200"],
  waiting_analyst: ["Needs analyst", "bg-amber-100 text-amber-900 dark:bg-amber-900/50 dark:text-amber-200"],
  escalated: ["Escalated", "bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-200"],
  verified: ["Verified", "bg-emerald-600 text-white"],
};
export function StatusChip({ status }: { status: string }) {
  const [label, cls] = STATUS[status] ?? [status, "bg-slate-200 text-slate-800"];
  return <span className={`inline-block whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}>{label}</span>;
}

export function Bar({ value, tone }: { value: number; tone?: "bad" | "good" | "" }) {
  const w = Math.max(0, Math.min(1, value)) * 100;
  const color = tone === "good" ? "bg-emerald-500" : tone === "bad" || value > 0.5 ? "bg-red-500" : value > 0.2 ? "bg-amber-500" : "bg-emerald-500";
  return (
    <div className="flex items-center gap-2" title={`${w.toFixed(0)}%`}>
      <div className="h-1.5 w-16 overflow-hidden rounded bg-slate-200 dark:bg-slate-700"><div className={`h-full ${color}`} style={{ width: `${w}%` }} /></div>
      <span className="w-9 text-right text-xs tabular-nums">{w.toFixed(0)}%</span>
    </div>
  );
}

export function Mono({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={`font-mono text-xs ${className}`}>{children}</span>;
}

export function Btn({ children, onClick, kind = "default", disabled, title }: {
  children: ReactNode; onClick?: () => void; kind?: "primary" | "good" | "danger" | "default"; disabled?: boolean; title?: string;
}) {
  const base = "rounded-md px-3 py-1.5 text-sm font-medium transition disabled:opacity-40";
  const k = {
    primary: "bg-indigo-600 text-white hover:bg-indigo-500",
    good: "bg-emerald-600 text-white hover:bg-emerald-500",
    danger: "border border-red-300 text-red-700 hover:bg-red-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950",
    default: "border border-slate-300 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800",
  }[kind];
  return <button className={`${base} ${k}`} onClick={onClick} disabled={disabled} title={title}>{children}</button>;
}

export function ErrorNote({ text }: { text: string }) {
  return text ? <pre className="mb-3 whitespace-pre-wrap rounded-md bg-red-50 p-2 text-xs text-red-800 dark:bg-red-950 dark:text-red-200">{text}</pre> : null;
}

export function usePolling(fn: () => void, ms: number, deps: unknown[] = []) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    ref.current();
    const t = setInterval(() => ref.current(), ms);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

export const hoursLabel = (h: number | null | undefined) =>
  h == null ? "-" : h < 0 ? `${Math.abs(h).toFixed(0)}h overdue` : h < 48 ? `${h.toFixed(0)}h` : `${(h / 24).toFixed(1)}d`;

export const fmtTime = (iso: string) => {
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
};
