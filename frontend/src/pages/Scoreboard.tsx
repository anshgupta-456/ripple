import { useState } from "react";
import { AdversarialSummary, api, BenchResults, PublicConfig, Rate, SysSummary } from "../api";
import { Btn, Card, ErrorNote, Mono, usePolling } from "../components/ui";

type Slice = "all" | "tuning" | "held_out";
const pct = (r: Rate) => (r.rate == null ? "n/a" : `${(r.rate * 100).toFixed(0)}%`);
const ci = (r: Rate) => (r.ci95 ? `95% CI ${(r.ci95[0] * 100).toFixed(0)}-${(r.ci95[1] * 100).toFixed(0)}%` : "");
const num = (v: number | null | undefined, d = 1) => (v == null ? "-" : v.toFixed(d));
const cell = "px-2 py-1.5 text-right tabular-nums";

function RateCell({ r, good }: { r: Rate; good?: "high" | "low" }) {
  const v = r.rate;
  const tone = v == null || !good ? "" : good === "high" ? (v >= 0.95 ? "text-emerald-600 dark:text-emerald-400" : v <= 0.5 ? "text-red-600 dark:text-red-400" : "")
    : (v === 0 ? "text-emerald-600 dark:text-emerald-400" : v > 0.1 ? "text-red-600 dark:text-red-400" : "");
  return <td className={`${cell} ${tone}`} title={`${r.k}/${r.n}  ${ci(r)}`}>{pct(r)} <span className="text-[11px] text-slate-400">{r.k}/{r.n}</span></td>;
}

function Head({ cols }: { cols: string[] }) {
  return <thead className="text-xs text-slate-500"><tr><th className="px-2 py-1.5 text-left">System</th>{cols.map((c) => <th key={c} className={cell}>{c}</th>)}</tr></thead>;
}

function Bars({ rows, pick, title }: { rows: [string, SysSummary][]; pick: (s: SysSummary) => Rate; title: string }) {
  return (
    <div>
      <p className="mb-1 text-xs font-medium text-slate-500">{title}</p>
      <div className="space-y-1">
        {rows.map(([k, v]) => {
          const r = pick(v);
          return (
            <div key={k} className="flex items-center gap-2 text-xs">
              <span className="w-5 font-semibold">{k}</span>
              <div className="h-3 flex-1 overflow-hidden rounded bg-slate-100 dark:bg-slate-800">
                <div className={`h-full ${k === "E" ? "bg-indigo-600" : "bg-slate-400"}`} style={{ width: `${(r.rate ?? 0) * 100}%` }} />
              </div>
              <span className="w-24 text-right tabular-nums">{pct(r)} <span className="text-slate-400">{r.k}/{r.n}</span></span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function candid(b: BenchResults): string[] {
  const S = b.summary.all, E = S.E, C = S.C;
  if (!E || !C) return [];
  const out: string[] = [];
  const ea = E.autonomous_resolution_rate.rate, ca = C.autonomous_resolution_rate.rate;
  if (ea != null && ca != null && Math.abs(ea - ca) < 0.005) out.push(`Autonomy is a tie: E ${pct(E.autonomous_resolution_rate)} vs C ${pct(C.autonomous_resolution_rate)}. The plain agent is just as good on easy cases; RIPPLE's edge is safety and truthful closure.`);
  if (E.cost_per_case_inr.mean != null && C.cost_per_case_inr.mean != null && E.cost_per_case_inr.mean > C.cost_per_case_inr.mean)
    out.push(`C is cheaper in the modeled cost (₹${num(C.cost_per_case_inr.mean)} vs ₹${num(E.cost_per_case_inr.mean)} per case) because it never waits for a person. The model does not price its ${C.unsafe_actions.total} policy violations or ${C.false_resolved.k} false closures.`);
  if (C.missed_obligations.avoidable <= E.missed_obligations.avoidable)
    out.push(`C missed no more deadlines than E (${C.missed_obligations.avoidable} vs ${E.missed_obligations.avoidable} avoidable): acting without approval is fast. That speed is exactly what the safety tiers trade away.`);
  out.push(`The benchmark is self-authored and ${b.meta.held_out_families.length} held-out families were visible in dev runs. Perfect E scores mostly show it passes its own tests.`);
  return out;
}

export default function Scoreboard() {
  const [b, setB] = useState<BenchResults | null>(null);
  const [cfg, setCfg] = useState<PublicConfig | null>(null);
  const [slice, setSlice] = useState<Slice>("all");
  const [adv, setAdv] = useState<AdversarialSummary | null>(null);
  const [err, setErr] = useState("");
  usePolling(() => {
    if (!b) api.bench().then(setB).catch((e) => setErr(String(e)));
    if (!cfg) api.config().then(setCfg).catch(() => {});
  }, 5000, [!!b, !!cfg]);

  if (err && !b) return <Card><p className="text-sm">No benchmark results yet. Run <Mono>python -m app.bench.run --label dev</Mono> in the backend folder.</p></Card>;
  if (!b) return <Card><p className="text-sm text-slate-500">Loading…</p></Card>;
  const rows = Object.entries(b.summary[slice]) as [string, SysSummary][];
  const fams = Object.entries(b.summary.by_family);
  const keys = Object.keys(b.meta.systems);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold">Scoreboard</h1>
        <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${b.meta.scored ? "bg-emerald-600 text-white" : "bg-amber-200 text-amber-900"}`}>{b.meta.scored ? "SCORED RUN" : "DEV RUN (not scored)"}</span>
        <span className="text-xs text-slate-500">“{b.meta.label}” · {b.meta.n_runs} runs ({b.meta.n_scenarios} scenarios × 5 systems) · config <Mono>{b.meta.config_hash}</Mono> v{b.meta.config_version} · commit <Mono>{b.meta.git_commit}</Mono>{b.meta.git_dirty && " (dirty)"} {b.meta.git_tags_at_head.map((t) => <Mono key={t} className="ml-1 rounded bg-indigo-100 px-1 text-indigo-800 dark:bg-indigo-900 dark:text-indigo-200">{t}</Mono>)}</span>
      </div>
      <p className="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-200">
        Synthetic, seeded scenarios. Costs and times are modeled from published assumptions. Rates show k/n; hover for 95% intervals. System C is a scripted proxy, not a live model.
      </p>
      <div className="flex gap-2 text-xs">
        {(["all", "tuning", "held_out"] as Slice[]).map((s) => (
          <button key={s} onClick={() => setSlice(s)} className={`rounded-full border px-3 py-1 ${slice === s ? "border-indigo-600 bg-indigo-600 text-white" : "border-slate-300 dark:border-slate-700"}`}>
            {s === "held_out" ? "held-out families" : s === "tuning" ? "tuning families" : "all"}</button>))}
        {slice === "held_out" && <span className="self-center text-slate-500">{b.meta.held_out_families.join(", ")} (see disclosures)</span>}
      </div>

      <Card title="Primary metrics (mapped to the problem statement)">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[52rem] text-sm">
            <Head cols={["Autonomous", "Verified", "Esc. precision", "Esc. recall", "False esc.", "Unsafe actions", "False “resolved”", "Obeyed attack"]} />
            <tbody>
              {rows.map(([k, v]) => (
                <tr key={k} className={`border-t border-slate-100 dark:border-slate-800 ${k === "E" ? "bg-indigo-50 font-semibold dark:bg-indigo-950/40" : ""}`}>
                  <td className="px-2 py-1.5">{k}. {v.label}</td>
                  <RateCell r={v.autonomous_resolution_rate} good="high" /><RateCell r={v.verified_resolution_rate} good="high" />
                  <RateCell r={v.escalation_precision} /><RateCell r={v.escalation_recall} good="high" /><RateCell r={v.false_escalation_rate} good="low" />
                  <td className={`${cell} ${v.unsafe_actions.total === 0 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`} title={JSON.stringify(v.unsafe_actions.by_kind)}>{v.unsafe_actions.total}</td>
                  <td className={`${cell} ${v.false_resolved.k === 0 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>{v.false_resolved.k}</td>
                  <RateCell r={v.injection_complied} good="low" />
                </tr>))}
            </tbody>
          </table>
        </div>
        <div className="mt-4 grid grid-cols-1 gap-6 md:grid-cols-2">
          <Bars rows={rows} pick={(s) => s.autonomous_resolution_rate} title="Autonomous resolution rate (safe cases, zero human touch)" />
          <Bars rows={rows} pick={(s) => s.verified_resolution_rate} title="Verified resolution rate (source-confirmed closure)" />
        </div>
        <p className="mt-3 text-xs text-slate-500">Unsafe action = a call that reached the tool layer without the authority policy demands (approval, consent, opt-in), a forbidden tool, or unsafe outbound text. Judged by an independent auditor, identically for every system. False “resolved” = closed or claimed resolved without source proof at that moment.</p>
      </Card>

      <Card title="Outcome and cost (modeled, not measured)">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[48rem] text-sm">
            <Head cols={["Missed (avoidable / unavoidable)", "Time to verified (median / p90)", "₹ per case", "Repeat contacts", "Avoided vs A", "Blind retries", "Minutes saved / escalation"]} />
            <tbody>
              {rows.map(([k, v]) => (
                <tr key={k} className={`border-t border-slate-100 dark:border-slate-800 ${k === "E" ? "bg-indigo-50 font-semibold dark:bg-indigo-950/40" : ""}`}>
                  <td className="px-2 py-1.5">{k}. {v.label}</td>
                  <td className={cell}>{v.missed_obligations.avoidable} / {v.missed_obligations.unavoidable} <span className="text-[11px] text-slate-400">of {v.missed_obligations.of}</span></td>
                  <td className={cell}>{num(v.time_to_verified_resolution_h.median)}h / {num(v.time_to_verified_resolution_h.p90)}h <span className="text-[11px] text-slate-400">n={v.time_to_verified_resolution_h.n}</span></td>
                  <td className={cell}>₹{num(v.cost_per_case_inr.mean)} <span className="text-[11px] text-slate-400">manual ₹{num(v.cost_per_case_inr.manual_baseline, 0)}</span></td>
                  <td className={cell}>{v.repeat_contacts}</td><td className={cell}>{v.repeat_contacts_avoided_vs_A ?? "-"}</td>
                  <td className={cell}>{v.blind_retries}</td><td className={cell}>{num(v.human_minutes_saved_per_escalated_case, 0)}</td>
                </tr>))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="Reasoning quality">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[44rem] text-sm">
            <Head cols={["Causal trace precision", "False causal claims", "Do-nothing correct", "Dedupe", "No blind write retry", "Decision stability"]} />
            <tbody>
              {rows.map(([k, v]) => (
                <tr key={k} className={`border-t border-slate-100 dark:border-slate-800 ${k === "E" ? "bg-indigo-50 font-semibold dark:bg-indigo-950/40" : ""}`}>
                  <td className="px-2 py-1.5">{k}. {v.label}</td>
                  <RateCell r={v.causal_trace_precision} /><td className={cell}>{v.false_causal_claims}</td>
                  <RateCell r={v.do_nothing_correctness} /><RateCell r={v.dedupe_ok} /><RateCell r={v.no_blind_write_retry} />
                  <td className={cell}>{v.decision_stability_mean != null ? `${(v.decision_stability_mean * 100).toFixed(0)}%` : "-"}</td>
                </tr>))}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs text-slate-500">Promise extraction: {(b.promise_eval.promise_detection_accuracy * 100).toFixed(0)}% detection, {(b.promise_eval.weekday_accuracy * 100).toFixed(0)}% weekday accuracy on {b.promise_eval.n} hand-labeled messages (small, same author; rules-only).</p>
      </Card>

      <Card title="Where each system wins and loses, by scenario family">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[44rem] text-xs">
            <thead className="text-slate-500"><tr><th className="py-1 pr-2 text-left">Family</th>{keys.map((k) => <th key={k} className="px-2 text-center">{k}</th>)}</tr></thead>
            <tbody>
              {fams.map(([f, v]) => (
                <tr key={f} className="border-t border-slate-100 dark:border-slate-800">
                  <td className="py-1.5 pr-2">{f.replaceAll("_", " ")}{b.meta.held_out_families.includes(f) && <span className="ml-1 text-slate-400">(held-out)</span>}</td>
                  {keys.map((k) => {
                    const c = v[k];
                    const bad = c.unsafe > 0 || c.false_resolved > 0 || c.missed > 0;
                    return (
                      <td key={k} className={`px-2 py-1 text-center tabular-nums ${bad ? "bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-300" : c.verified === c.n ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300" : ""}`}
                        title={`verified ${c.verified}/${c.n}, handed to human ${c.handoff}, false resolved ${c.false_resolved}, unsafe ${c.unsafe}, missed avoidable ${c.missed}`}>
                        V{c.verified} H{c.handoff}{c.false_resolved > 0 && ` F${c.false_resolved}`}{c.unsafe > 0 && ` U${c.unsafe}`}{c.missed > 0 && ` M${c.missed}`}
                      </td>);
                  })}
                </tr>))}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs text-slate-500">V verified closures, H handed to a human, F false “resolved”, U unsafe actions, M avoidable missed deadlines (out of the n runs per family). Red = a safety or outcome failure.</p>
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Where RIPPLE does not win">
          <ul className="list-disc space-y-1.5 pl-5 text-sm">{candid(b).map((t) => <li key={t}>{t}</li>)}</ul>
        </Card>
        <Card title="Safety suite" right={<Btn onClick={() => api.adversarial().then(setAdv).catch((e) => setErr(String(e)))}>Run adversarial suite</Btn>}>
          <p className="text-sm">Frozen run: <b>{b.adversarial.action_cases - b.adversarial.unsafe_actions}/{b.adversarial.action_cases}</b> attack cases blocked, {b.adversarial.screening_cases} injection-screening phrases checked.</p>
          {adv && <p className="mt-2 text-sm">Live run now: <b className={adv.unsafe_actions === 0 ? "text-emerald-600" : "text-red-600"}>{adv.action_cases - adv.unsafe_actions}/{adv.action_cases} blocked</b>, {adv.screening_misses.length} screening misses, {adv.screening_false_positives.length} false positives.</p>}
          <p className="mt-2 text-xs text-slate-500">Includes forged/replayed/expired approvals, forbidden tools, parameter smuggling, bank-detail text, idempotency-key swaps and fake-authority messages. A mutation test proves the suite fails when the firewall is weakened.</p>
        </Card>
      </div>

      <Card title="Modeled assumptions (all in config/frozen.yaml)">
        {cfg ? (
          <div className="grid gap-4 text-xs md:grid-cols-3">
            <div><p className="mb-1 font-semibold">Cost model</p><ul className="space-y-0.5">{Object.entries(cfg.cost_model).map(([k, v]) => <li key={k}>{k.replaceAll("_", " ")}: <b>{v}</b></li>)}</ul></div>
            <div><p className="mb-1 font-semibold">Harm weights</p><ul className="space-y-0.5">{Object.entries(cfg.harm_weights).map(([k, v]) => <li key={k}>{k.replaceAll("_", " ")}: <b>{v}</b></li>)}</ul></div>
            <div><p className="mb-1 font-semibold">Simulation priors</p><ul className="space-y-0.5">
              <li>review time: {cfg.engine.review_hours.mean}h ± {cfg.engine.review_hours.sd}</li><li>release delay: {cfg.engine.release_delay_hours.mean}h ± {cfg.engine.release_delay_hours.sd}</li>
              <li>analyst response median: {cfg.engine.approval_latency_hours.median}h</li><li>supplier accepts notice: {(cfg.engine.supplier_notice.accept_prob * 100).toFixed(0)}%</li></ul></div>
          </div>) : <p className="text-sm text-slate-500">Loading…</p>}
      </Card>

      <Card title="Disclosures">
        <ul className="list-disc space-y-1 pl-5 text-xs text-slate-600 dark:text-slate-300">
          {b.meta.notes.map((n) => <li key={n}>{n}</li>)}
          <li>Dev results were inspected before the freeze; changes made and their reasons are in docs/BENCHMARK.md.</li>
        </ul>
      </Card>
      <ErrorNote text={err} />
    </div>
  );
}
