import { useState } from "react";
import { api, PendingApproval } from "../api";
import { Btn, Card, ErrorNote, Mono, hoursLabel, usePolling } from "../components/ui";

const ALL_DOCS = ["invoice", "delivery_proof", "customer_comm", "bank_statement", "courier_receipt"];

function ApprovalCard({ item, onDone }: { item: PendingApproval; onDone: (msg: string) => void }) {
  const m = item.memo;
  const [editing, setEditing] = useState(false);
  const [docs, setDocs] = useState<string[]>(item.bundle?.docs ?? []);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const run = async (fn: () => Promise<string>) => {
    setBusy(true);
    try { setErr(""); onDone(await fn()); } catch (e) { setErr(String(e)); } finally { setBusy(false); }
  };
  const approve = () => run(async () => {
    const c = await api.caseDecide(item.action.hash, true, item.sid);
    const ev = c.plan.find((p) => p.action.type === "submit_dispute_evidence");
    return `${item.merchant}: ${ev?.state ?? "done"} (${ev?.ladder.join(" → ") || "no steps"})${ev?.state === "failed" ? " - source rejected it, escalated" : ""}`;
  });
  const reject = () => run(async () => { await api.caseDecide(item.action.hash, false, item.sid); return `${item.merchant}: rejected, escalated to a human`; });
  const saveEdit = () => run(async () => { await api.caseEdit(item.action.hash, docs, item.sid); setEditing(false); return `${item.merchant}: bundle edited, new action hash, fresh approval needed`; });

  return (
    <Card>
      <div className="flex flex-wrap items-start gap-2">
        <div>
          <h3 className="text-base font-semibold">{item.merchant}</h3>
          <p className="text-xs text-slate-500">{item.label} · tier {item.tier} (human approval) · <a className="text-indigo-600 underline" href={`#/case/${item.sid}`}>open case</a></p>
        </div>
        <div className="ml-auto text-right text-sm">
          <p className={`font-semibold tabular-nums ${item.hours_to_deadline != null && item.hours_to_deadline < 12 ? "text-red-600" : ""}`}>{hoursLabel(item.hours_to_deadline)} to deadline</p>
          {m && <p className="text-xs text-emerald-700 dark:text-emerald-400">saves ~{m.minutes_saved} min of analyst work (modeled)</p>}
        </div>
      </div>
      {m ? (
        <div className="mt-3 grid gap-4 lg:grid-cols-2">
          <div className="space-y-2 text-sm">
            <p><b>Merchant asked:</b> “{m.merchant_ask}”</p>
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Verified facts</p>
              <ul className="mt-1 space-y-0.5 text-xs">
                {m.facts.map((f) => <li key={f.label}><b>{f.label}:</b> {f.value} <Mono className="text-slate-500">[{f.source_ref}]</Mono></li>)}
              </ul>
            </div>
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Traced cause</p>
              <p className="mt-1 flex flex-wrap items-center gap-1 text-xs">
                {m.cause_path.slice().reverse().map((n, i, a) => (
                  <span key={n} className="flex items-center gap-1"><Mono className="rounded bg-slate-100 px-1.5 py-0.5 dark:bg-slate-800">{n}</Mono>{i < a.length - 1 && <span>→</span>}</span>))}
              </p>
              {m.rejected_edges.map((e, i) => (
                <p key={i} className="mt-1 rounded border border-dashed border-amber-400 px-2 py-1 text-xs">Not causal: <Mono>{e.src}</Mono>. {e.note}</p>))}
            </div>
            <p className="text-sm"><b>If nothing is done:</b> {(m.if_nothing_p_miss * 100).toFixed(0)}% chance the supplier payment is missed.</p>
          </div>
          <div className="space-y-2 text-sm">
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Evidence bundle {item.bundle && <span className="font-normal normal-case">for {item.bundle.dispute_id}</span>}</p>
              {!editing ? (
                <p className="mt-1 text-xs">{(item.bundle?.docs ?? []).join(", ") || "-"} <span className="text-slate-500">· {item.bundle?.provenance_note}</span></p>
              ) : (
                <div className="mt-1 space-y-1 text-xs">
                  {ALL_DOCS.map((d) => (
                    <label key={d} className="flex items-center gap-2">
                      <input type="checkbox" checked={docs.includes(d)} onChange={(e) => setDocs(e.target.checked ? [...docs, d] : docs.filter((x) => x !== d))} />{d}
                    </label>))}
                  <p className="text-slate-500">Dropping a required document is allowed; the source will reject an incomplete bundle and the agent will escalate.</p>
                </div>
              )}
            </div>
            <table className="w-full text-left text-xs">
              <thead className="text-slate-500"><tr><th>Ranked options</th><th>Tier</th><th>P(miss)</th><th>Score</th></tr></thead>
              <tbody>
                {m.options.map((o) => (
                  <tr key={o.id} className={o.chosen ? "font-semibold" : ""}><td className="py-0.5 pr-2">{o.label}</td><td>{o.tier}</td><td>{(o.p_miss * 100).toFixed(0)}%</td><td>{o.score.toFixed(3)}</td></tr>))}
              </tbody>
            </table>
            <p className="text-xs text-slate-500">Approval is bound to action hash <Mono>{item.action.hash.slice(0, 16)}…</Mono>, this case, and a 60-minute expiry. It works once. Editing creates a different hash.</p>
          </div>
        </div>
      ) : <p className="mt-2 text-sm text-slate-500">No memo attached.</p>}
      <ErrorNote text={err} />
      <div className="mt-3 flex flex-wrap gap-2 border-t border-slate-100 pt-3 dark:border-slate-800">
        <Btn kind="good" disabled={busy || editing} onClick={approve}>Approve</Btn>
        {!editing ? <Btn disabled={busy} onClick={() => setEditing(true)}>Edit bundle</Btn>
          : <><Btn kind="primary" disabled={busy || docs.length === 0} onClick={saveEdit}>Save edit</Btn><Btn disabled={busy} onClick={() => { setEditing(false); setDocs(item.bundle?.docs ?? []); }}>Cancel</Btn></>}
        <Btn kind="danger" disabled={busy || editing} onClick={reject}>Reject</Btn>
      </div>
    </Card>
  );
}

export default function Approvals() {
  const [items, setItems] = useState<PendingApproval[]>([]);
  const [log, setLog] = useState<string[]>([]);
  const [err, setErr] = useState("");
  const load = () => api.pending().then(setItems).catch((e) => setErr(String(e)));
  usePolling(load, 3000);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline gap-2">
        <h1 className="text-xl font-semibold">Approvals</h1>
        <span className="text-sm text-slate-500">Tier 3 actions wait here. Everything is prepared; you decide.</span>
      </div>
      <ErrorNote text={err} />
      {log.length > 0 && (
        <Card title="Just decided">
          <ul className="space-y-1 text-sm">{log.slice(0, 5).map((l, i) => <li key={i}>{l}</li>)}</ul>
        </Card>
      )}
      {items.length === 0 ? (
        <Card><p className="py-8 text-center text-sm text-slate-500">Nothing is waiting for approval. Load the demo portfolio on the Ops Console to see cases that need you.</p></Card>
      ) : items.map((it) => <ApprovalCard key={it.action.hash} item={it} onDone={(msg) => { setLog((l) => [msg, ...l]); load(); }} />)}
    </div>
  );
}
