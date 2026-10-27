import { useEffect, useState } from "react";
import { api, BenchResults, Rate } from "../api";

const pct = (r: Rate) => (r.rate == null ? "n/a" : `${(r.rate * 100).toFixed(0)}%`);
const frac = (r: Rate) => `${r.k}/${r.n}`;

export default function ScoreboardCard() {
  const [b, setB] = useState<BenchResults | null>(null);
  const [slice, setSlice] = useState<"all" | "tuning" | "held_out">("all");
  const [err, setErr] = useState("");
  useEffect(() => { api.bench().then(setB).catch((e) => setErr(String(e))); }, []);
  if (err) return <p className="text-sm text-slate-500">No benchmark results yet ({err.slice(0, 80)}).</p>;
  if (!b) return <p className="text-sm text-slate-500">Loading…</p>;
  const rows = Object.entries(b.summary[slice]);
  const cell = "px-2 py-1 text-right tabular-nums";
  return (
    <div className="space-y-2 text-sm">
      <p className="text-xs text-slate-500">
        {b.meta.scored ? "SCORED" : "DEV (unscored)"} run “{b.meta.label}” · {b.meta.n_runs} runs · config {b.meta.config_hash} ·
        commit {b.meta.git_commit}{b.meta.git_dirty ? " (dirty)" : ""} {b.meta.git_tags_at_head.join(",")} · synthetic data, modeled costs
      </p>
      <div className="flex gap-2 text-xs">
        {(["all", "tuning", "held_out"] as const).map((s) => (
          <button key={s} className={`rounded border px-2 py-0.5 ${slice === s ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900" : ""}`} onClick={() => setSlice(s)}>{s}</button>
        ))}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-slate-500">
            <tr><th className="px-2 py-1 text-left">System</th><th className={cell}>Autonomous</th><th className={cell}>Verified</th>
              <th className={cell}>Esc. prec</th><th className={cell}>Esc. recall</th><th className={cell}>False esc.</th>
              <th className={cell}>Unsafe</th><th className={cell}>False “resolved”</th><th className={cell}>₹/case</th></tr>
          </thead>
          <tbody>
            {rows.map(([k, v]) => (
              <tr key={k} className={`border-t border-slate-200 dark:border-slate-800 ${k === "E" ? "font-semibold" : ""}`}>
                <td className="px-2 py-1">{k}. {v.label}</td>
                <td className={cell}>{pct(v.autonomous_resolution_rate)} <span className="text-slate-500">{frac(v.autonomous_resolution_rate)}</span></td>
                <td className={cell}>{pct(v.verified_resolution_rate)} <span className="text-slate-500">{frac(v.verified_resolution_rate)}</span></td>
                <td className={cell}>{pct(v.escalation_precision)}</td>
                <td className={cell}>{pct(v.escalation_recall)}</td>
                <td className={cell}>{pct(v.false_escalation_rate)}</td>
                <td className={`${cell} ${v.unsafe_actions.total === 0 ? "text-emerald-700" : "text-red-700"}`}>{v.unsafe_actions.total}</td>
                <td className={`${cell} ${v.false_resolved.k === 0 ? "text-emerald-700" : "text-red-700"}`}>{v.false_resolved.k}</td>
                <td className={cell}>{v.cost_per_case_inr.mean ?? "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="list-disc pl-5 text-xs text-slate-500">
        {b.meta.notes.map((n) => <li key={n}>{n}</li>)}
        <li>Adversarial suite: {b.adversarial.action_cases - b.adversarial.unsafe_actions}/{b.adversarial.action_cases} blocked. Promise extraction: {(b.promise_eval.promise_detection_accuracy * 100).toFixed(0)}% on {b.promise_eval.n} hand-labeled messages.</li>
        <li>Held-out families: {b.meta.held_out_families.join(", ")} (visible in dev runs; see docs/BENCHMARK.md).</li>
      </ul>
    </div>
  );
}
