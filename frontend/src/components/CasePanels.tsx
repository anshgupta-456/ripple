import { useState } from "react";
import { AuditEntry, CaseView, RankedAction, Report } from "../api";
import { Bar, Mono, fmtTime } from "./ui";

export function RankedTable({ ranked, chosenId }: { ranked: RankedAction[]; chosenId: string }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[34rem] text-left text-xs">
        <thead className="text-slate-500">
          <tr><th className="py-1 pr-2">Action</th><th className="pr-2">Tier</th><th className="pr-2">P(miss)</th><th className="pr-2">E[harm]</th>
            <th className="pr-2">Score</th><th className="pr-2">Release p50</th><th>Why</th></tr>
        </thead>
        <tbody>
          {ranked.map((r) => {
            const out = !r.permitted || !r.feasible;
            return (
              <tr key={r.id} className={`border-t border-slate-100 dark:border-slate-800 ${r.id === chosenId ? "bg-indigo-50 font-semibold dark:bg-indigo-950/40" : ""} ${out || r.decoy ? "text-slate-400" : ""}`}>
                <td className={`py-1.5 pr-2 ${r.decoy ? "line-through decoration-dashed" : ""}`}>{r.label}{r.id === chosenId && <span className="ml-1 rounded bg-indigo-600 px-1 text-[10px] font-medium text-white">chosen</span>}</td>
                <td className="pr-2">{r.id === "DO_NOTHING" ? "-" : r.tier}</td>
                <td className="pr-2"><Bar value={r.p_miss} /></td>
                <td className="pr-2 tabular-nums">{r.expected_harm.toFixed(3)}</td>
                <td className="pr-2 tabular-nums">{r.score.toFixed(3)}</td>
                <td className="pr-2 tabular-nums">{r.release_p50_h != null ? `${r.release_p50_h.toFixed(0)}h` : "-"}</td>
                <td className="font-normal text-slate-500">{r.decoy && !out ? "decoy: no causal path" : out ? r.reason : ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function WhyDrawer({ r }: { r: Report }) {
  const [open, setOpen] = useState(false);
  const p = r.params;
  return (
    <div>
      <button className="text-sm font-medium text-indigo-600 underline" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "Hide" : "Why?"} how this was decided</button>
      {open && (
        <div className="mt-2 space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs dark:border-slate-700 dark:bg-slate-950">
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
            <dt className="text-slate-500">Graph</dt><dd>version {r.graph.version}, digest <Mono>{r.graph.digest}</Mono>, {r.graph.nodes.length} nodes, {r.graph.edges.length} edges</dd>
            <dt className="text-slate-500">Simulation</dt><dd>seed <Mono>{r.seed}</Mono>, {r.n_samples.toLocaleString()} samples, same seed gives the same decision</dd>
            <dt className="text-slate-500">Stability</dt><dd>{(r.stability * 100).toFixed(0)}% of ±20% parameter perturbations pick the same action</dd>
            <dt className="text-slate-500">Priors</dt>
            <dd>review ~{p.review_hours?.mean}h ± {p.review_hours?.sd}, release ~{p.release_delay_hours?.mean}h, approval median {p.approval_latency_hours?.median}h, notice accepted {(p.supplier_notice?.accept_prob * 100).toFixed(0)}% (all modeled)</dd>
            <dt className="text-slate-500">Policy result</dt><dd>disposition <b>{r.disposition}</b>{r.triggers.length > 0 && <>, triggers: {r.triggers.join(", ")}</>}{r.conflicts.length > 0 && <>, conflicts: {r.conflicts.join("; ")}</>}</dd>
            <dt className="text-slate-500">Trace</dt><dd>{r.trace.status}; only confirmed and obligation edges were walked; {r.trace.rejected_edges.length} correlated edge(s) rejected</dd>
          </dl>
          <p className="text-slate-500">The model extracted the message and drafted replies. It did not score, gate, execute, invent edges or declare any status; those are deterministic code.</p>
        </div>
      )}
    </div>
  );
}

const LADDER = ["attempted", "submitted", "approved", "executed", "verified"];
export function StateLadder({ plan }: { plan: CaseView["plan"] }) {
  if (!plan.length) return <p className="text-sm text-slate-500">No actions planned yet.</p>;
  return (
    <div className="space-y-3">
      {plan.map((p) => (
        <div key={p.action.hash} className="text-sm">
          <p className="mb-1 flex flex-wrap items-center gap-2"><b>{p.label}</b><span className="text-xs text-slate-500">tier {p.tier} · {p.state.replaceAll("_", " ")}</span>
            <span className="text-xs text-slate-500">key <Mono>{p.action.idempotency_key}</Mono></span></p>
          <ol className="flex flex-wrap gap-1">
            {LADDER.map((s) => {
              const done = p.ladder.includes(s);
              const na = (s === "submitted" || s === "approved") && p.tier === 1;
              return <li key={s} className={`rounded-full px-2 py-0.5 text-xs ${na ? "hidden" : done ? (s === "verified" ? "bg-emerald-600 text-white" : "bg-indigo-600 text-white") : "border border-slate-300 text-slate-400 dark:border-slate-700"}`}>{s}</li>;
            })}
          </ol>
          {p.note && <p className="mt-1 text-xs text-red-600">{p.note}</p>}
        </div>
      ))}
    </div>
  );
}

export function ActionLog({ audit }: { audit: AuditEntry[] }) {
  // hide routine noise: source reads and the agent's own chat replies (denied replies are still shown)
  const at = (e: AuditEntry) => String(e.data.action_type ?? "");
  const shown = audit.filter((e) => {
    if (e.kind === "firewall") return !at(e).startsWith("read_") && !(at(e) === "send_merchant_message" && e.outcome === "allow");
    if (e.kind === "tool_call") return at(e) !== "send_merchant_message";
    if (e.kind === "verification") return e.data.reason !== "message in notification log";
    return true;
  });
  return (
    <div className="max-h-72 overflow-auto">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-white text-left text-slate-500 dark:bg-slate-900"><tr><th className="py-1 pr-2">#</th><th className="pr-2">Kind</th><th className="pr-2">Outcome</th><th className="pr-2">Detail</th><th>Action</th></tr></thead>
        <tbody>
          {shown.map((e) => (
            <tr key={e.seq} className="border-t border-slate-100 align-top dark:border-slate-800">
              <td className="py-1 pr-2 text-slate-500">{e.seq}</td>
              <td className="pr-2">{e.kind}</td>
              <td className={`pr-2 font-medium ${e.outcome === "deny" || e.outcome === "failed" || e.outcome === "timeout" ? "text-red-600" : e.outcome === "allow" || e.outcome === "ok" || e.outcome === "verified" || e.outcome === "issued" ? "text-emerald-600" : ""}`}>{e.outcome}</td>
              <td className="pr-2">{e.code || String(e.data.reason ?? e.data.approver ?? "")}</td>
              <td><Mono>{String(e.data.action_type ?? "")}</Mono> {e.action_hash && <Mono className="text-slate-400">{e.action_hash.slice(0, 8)}</Mono>}</td>
            </tr>
          ))}
          {!shown.length && <tr><td colSpan={5} className="py-3 text-slate-500">No actions yet.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

export function ContextPanel({ world, events }: { world: Record<string, any>; events: { id: string; ts: string; source: string; type: string; source_ref: string }[] }) {
  const s = world.settlement, d = world.dispute, k = world.kyc, sp = world.supplier, rf = world.refund;
  return (
    <div className="grid grid-cols-1 gap-3 text-xs md:grid-cols-2">
      <div className="space-y-2">
        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">Obligations</p>
        {sp ? <p>Pay <b>{sp.name}</b> Rs {sp.amount.toLocaleString("en-IN")} by <b>{fmtTime(sp.due_at)}</b>{sp.notices_sent > 0 && <span className="text-emerald-600"> · notice sent</span>}</p> : <p className="text-slate-500">None known</p>}
        {s && <p>Settlement <b>{s.status.replace("_", " ")}</b>, Rs {s.amount_total.toLocaleString("en-IN")}{s.held_amount > 0 && ` (Rs ${s.held_amount.toLocaleString("en-IN")} held: ${s.hold_reason_code})`}</p>}
        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">Open cases at sources</p>
        {d && <p>Dispute {d.dispute_id}: <b>{d.status.replaceAll("_", " ")}</b>, evidence due {fmtTime(d.evidence_due)}</p>}
        {rf && <p>Refund {rf.refund_id}: <b>{rf.status}</b>, Rs {rf.amount.toLocaleString("en-IN")}</p>}
        {k && <p>KYC: <b>{k.status}</b>{k.expires_at && `, expires ${fmtTime(k.expires_at)}`}{k.opted_in_reminders ? ", reminders opted in" : ""}</p>}
        {!d && !rf && <p className="text-slate-500">No dispute or refund open</p>}
      </div>
      <div>
        <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">Source timeline</p>
        <ul className="max-h-40 space-y-0.5 overflow-auto">
          {events.map((e) => <li key={e.id}><span className="text-slate-500">{fmtTime(e.ts)}</span> <b>{e.source}.{e.type}</b></li>)}
          {!events.length && <li className="text-slate-500">No source events yet</li>}
        </ul>
      </div>
    </div>
  );
}


export function LedgerPanel({ rows }: { rows: import("../api").LedgerRow[] }) {
  if (!rows.length) return <p className="text-sm text-slate-500">No promises tracked yet. A commitment the merchant makes in chat (“Tuesday ko supplier ko paisa dena hai”) becomes an entry here.</p>;
  const tone: Record<string, string> = { at_risk: "text-amber-700", on_track: "text-emerald-700", repaired: "text-sky-700", closed: "text-emerald-700", unfunded: "text-red-700" };
  return (
    <table className="w-full text-left text-sm">
      <thead className="text-xs uppercase text-slate-500"><tr><th className="pr-2">To</th><th className="pr-2">Amount</th><th className="pr-2">Due</th><th className="pr-2">Confidence</th><th className="pr-2">Funded by</th><th>Status</th></tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.id} className="border-t border-slate-100 dark:border-slate-800">
          <td className="py-1 pr-2">{r.counterparty ?? "-"}</td><td className="pr-2">{r.amount ? `Rs ${r.amount.toLocaleString("en-IN")}` : "-"}</td>
          <td className="pr-2">{r.due_at ? r.due_at.slice(0, 16).replace("T", " ") : r.due_text ?? "-"}</td>
          <td className="pr-2">{(r.confidence * 100).toFixed(0)}%{r.confirmed ? "" : " (to confirm)"}</td>
          <td className="pr-2 font-mono text-xs">{r.funded_by ?? "none"}</td><td className={`font-semibold ${tone[r.status] ?? ""}`}>{r.status.replace("_", " ")}</td>
        </tr>))}</tbody>
    </table>
  );
}

export function RecordsPanel({ decisions, outcomes, followups }: { decisions: import("../api").DecisionRow[]; outcomes: import("../api").OutcomeRow[]; followups: import("../api").FollowupRow[] }) {
  if (!decisions.length) return <p className="text-sm text-slate-500">No decisions recorded yet.</p>;
  return (
    <div className="space-y-3 text-sm">
      <table className="w-full text-left"><thead className="text-xs uppercase text-slate-500"><tr><th className="pr-2">#</th><th className="pr-2">Chosen</th><th className="pr-2">Tier</th><th className="pr-2">Seed</th><th className="pr-2">Graph v</th><th className="pr-2">Stability</th><th>P(miss | nothing)</th></tr></thead>
        <tbody>{decisions.map((d, i) => (
          <tr key={d.decision_id} className="border-t border-slate-100 dark:border-slate-800"><td className="py-1 pr-2">{i + 1}</td><td className="pr-2">{d.chosen.replaceAll("_", " ").toLowerCase()}</td><td className="pr-2">{d.tier_result.replace("tier_", "")}</td>
            <td className="pr-2 font-mono text-xs">{d.seed}</td><td className="pr-2">{d.graph_version}</td><td className="pr-2">{(d.stability * 100).toFixed(0)}%</td><td>{(d.p_miss * 100).toFixed(0)}%</td></tr>))}</tbody></table>
      {outcomes.length > 0 && <div><p className="mb-1 text-xs font-semibold uppercase text-slate-500">Outcomes</p>
        <ul className="space-y-0.5">{outcomes.map((o, i) => <li key={i}><b>{o.action_type.replaceAll("_", " ")}</b>: risk {o.risk_before === null ? "-" : `${(o.risk_before * 100).toFixed(0)}%`} → {o.risk_after === null ? "-" : `${(o.risk_after * 100).toFixed(0)}%`}, <b>{o.verdict}</b></li>)}</ul></div>}
      {followups.length > 0 && <p className="text-xs text-slate-500">Internal rechecks: {followups.map((f) => `${f.reason.replace("_", " ")} at ${f.due_at.slice(0, 16).replace("T", " ")} (${f.status})`).join("; ")}</p>}
    </div>
  );
}


export function CascadePanel({ c }: { c: import("../api").Cascade }) {
  return (
    <div className="space-y-2 text-sm">
      <table className="w-full text-left"><thead className="text-xs uppercase text-slate-500"><tr><th className="pr-2">Downstream</th><th className="pr-2">Lag</th><th className="pr-2">Passes on</th><th className="pr-2">Miss if nothing is done</th><th>With this plan</th></tr></thead>
        <tbody>{c.nodes.map((n) => (<tr key={n.id} className="border-t border-slate-100 dark:border-slate-800"><td className="py-1 pr-2">{n.label} (Rs {n.amount.toLocaleString("en-IN")})</td><td className="pr-2">{n.lag_days} d</td><td className="pr-2">{(n.p_propagate * 100).toFixed(0)}%</td><td className="pr-2">{(n.p_miss_do_nothing * 100).toFixed(0)}%</td><td>{(n.p_miss_chosen * 100).toFixed(0)}%</td></tr>))}</tbody></table>
      <p className="text-xs text-slate-500">Expected EMI-bounce penalty: Rs {c.expected_emi_penalty_do_nothing} if nothing is done, Rs {c.expected_emi_penalty_chosen} with this plan. {c.assumption}.</p>
    </div>
  );
}

export function ExtrasPanel({ c }: { c: import("../api").CaseView }) {
  const b = c.extras?.bridge, u = c.extras?.udhaar;
  if (!b && !u) return <p className="text-sm text-slate-500">Offers and recovery are considered only when the main rescue cannot save the supplier deadline.</p>;
  return (
    <div className="space-y-3 text-sm">
      {b && <div><p className="text-xs font-semibold uppercase text-slate-500">Bridge offer</p>
        {b.offered ? <p>Offered {b.kind?.replaceAll("_", " ")} of Rs {b.amount?.toLocaleString("en-IN")}, fee Rs {b.fee?.toLocaleString("en-IN")}, expected harm avoided Rs {b.value?.toLocaleString("en-IN")}. Needs the merchant's explicit tap.</p>
          : <p>No offer: {b.reason}.</p>}</div>}
      {u && <div><p className="text-xs font-semibold uppercase text-slate-500">Udhaar recovery</p>
        {u.skip && <p>{u.skip}.</p>}
        {u.candidates?.map((x) => <p key={x.debtor_id}>{x.label} to <b>{x.name}</b> (Rs {x.amount.toLocaleString("en-IN")} overdue), score {x.score}.</p>)}
        {u.vetoes?.map((v) => <p key={v.name} className="text-amber-700">Human decides for {v.name}: {v.reason}.</p>)}</div>}
    </div>
  );
}

export function VoicePanel({ rows, online, onToggle }: { rows: import("../api").VoiceRow[]; online: boolean | null | undefined; onToggle?: () => void }) {
  return (
    <div className="space-y-2 text-sm">
      <div className="flex items-center gap-2"><span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${online ? "bg-emerald-100 text-emerald-800" : "bg-slate-200 text-slate-700"}`}>Soundbox {online ? "online" : "offline"}</span>
        {onToggle && <button className="rounded-md border border-slate-300 px-2 py-0.5 text-xs dark:border-slate-700" onClick={onToggle}>Take {online ? "offline" : "online"} (demo)</button>}</div>
      {rows.length === 0 ? <p className="text-slate-500">No messages yet.</p> :
        <ul className="space-y-1">{rows.slice(-6).map((r) => <li key={r.id}><span className={`mr-1 rounded px-1.5 text-xs font-semibold ${r.channel === "soundbox" ? "bg-indigo-100 text-indigo-800" : "bg-slate-200 text-slate-700"}`}>{r.channel === "soundbox" ? "Soundbox" : "App only"}</span>{r.kind.replaceAll("_", " ")}: <span className="text-slate-500">{r.reason}</span></li>)}</ul>}
      <p className="text-xs text-slate-500">In-app text is always delivered. Voice follows quiet hours (22:00 to 08:00), a daily cap, and falls back to the app when the device is offline.</p>
    </div>
  );
}
