import { useState } from "react";
import { api, AuditEntry, CaseView, Report, SessionState } from "../api";
import ChatPanel from "../components/ChatPanel";
import { ActionLog, ContextPanel, LedgerPanel, RankedTable, RecordsPanel, StateLadder, WhyDrawer } from "../components/CasePanels";
import TraceGraph from "../components/TraceGraph";
import { useAuth } from "../auth";
import { Btn, Card, ErrorNote, Mono, StatusChip, usePolling } from "../components/ui";

export default function CasePage({ sid }: { sid: string }) {
  const { can } = useAuth();
  const [verify, setVerify] = useState("");
  const [c, setC] = useState<CaseView | null>(null);
  const [st, setSt] = useState<SessionState | null>(null);
  const [events, setEvents] = useState<{ id: string; ts: string; source: string; type: string; source_ref: string }[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [fresh, setFresh] = useState<Report | null>(null);
  const [err, setErr] = useState("");
  const [actErr, setActErr] = useState("");     // errors from user actions stay until the next action (polling must not erase them)

  const load = () =>
    Promise.all([api.caseGet(sid), api.state(sid), api.events(sid), can("audit.read") ? api.audit(sid) : Promise.resolve([] as AuditEntry[])])
      .then(([cv, s, ev, au]) => { setC(cv); setSt(s); setEvents(ev); setAudit(au); setErr(""); })
      .catch((e) => setErr(String(e)));
  usePolling(load, 3000, [sid]);

  const act = async (fn: () => Promise<unknown>) => {
    try { setActErr(""); await fn(); setFresh(null); await load(); } catch (e) { setActErr(String(e)); }
  };
  const reanalyze = () => fetch(`/api/session/analyze?sid=${sid}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    .then((r) => r.json()).then(setFresh).catch((e) => setErr(String(e)));

  if (err && !c) return <Card><p className="mb-2 text-sm">{err}</p><a className="text-indigo-600 underline" href="#/">Back to the Ops Console</a></Card>;
  const report = fresh ?? c?.report ?? null;
  const approval = c?.plan.find((p) => p.state === "awaiting_approval");
  const evidence = c?.plan.find((p) => p.action.type === "submit_dispute_evidence");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <a className="text-sm text-indigo-600 underline" href="#/">← Ops Console</a>
        <h1 className="text-xl font-semibold">{c?.merchant_name ?? "Case"}</h1>
        {c && <StatusChip status={c.status} />}
        <span className="text-xs text-slate-500">{st?.scenario.description}</span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <span className="text-xs text-slate-500">Simulated time <Mono>{st?.world.now.slice(0, 16).replace("T", " ")}</Mono></span>
          {can("demo.control") && [6, 20, 26].map((h) => <Btn key={h} onClick={() => act(() => api.advance(h, sid))}>+{h}h</Btn>)}
        </div>
      </div>
      <ErrorNote text={err} />
      {actErr && <div role="alert" className="flex items-start gap-2"><div className="flex-1"><ErrorNote text={actErr} /></div><Btn onClick={() => setActErr("")}>Dismiss</Btn></div>}
      {c && c.unsafe.length > 0 && <ErrorNote text={`Independent audit found ${c.unsafe.length} unsafe tool call(s): ${c.unsafe.map((u) => u.kind).join(", ")}`} />}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <div className="min-w-0 space-y-4">
          <ChatPanel c={c} inbound={st?.scenario.inbound.text ?? ""} canSend={can("case.message")} onSend={(t) => act(() => api.caseMessage(t, sid))}
            onConsent={(h, a) => act(() => api.caseConsent(h, a, sid))} />
          <Card title="Agent steps">
            <ol className="max-h-60 space-y-1 overflow-auto text-xs">
              {c?.steps.map((s, i) => <li key={i}><b>{s.label}</b> <span className="text-slate-500">{s.detail}</span></li>)}
              {!c?.steps.length && <li className="text-slate-500">none yet</li>}
            </ol>
          </Card>
        </div>

        <div className="min-w-0 space-y-4">
          <Card title="Context">{st ? <ContextPanel world={st.world} events={events} /> : <p className="text-sm text-slate-500">Loading…</p>}</Card>

          <Card title="Traced cause" right={<Btn onClick={reanalyze} title="Re-run the engine on the current world state (takes no action)">Re-analyze now</Btn>}>
            {report ? (
              <div className="space-y-2">
                <p className="text-xs text-slate-500">
                  {report.trace.status.replaceAll("_", " ")} · P(miss | do nothing) <b>{(report.p_miss_do_nothing * 100).toFixed(0)}%</b>
                  {report.due_hours != null && ` · ${report.due_hours.toFixed(0)}h to deadline`}{fresh && " · fresh analysis"}
                </p>
                <TraceGraph nodes={report.graph.nodes} edges={report.graph.edges} path={report.trace.path} />
                {report.trace.rejected_edges.map((e, i) => (
                  <p key={i} className="rounded border border-dashed border-amber-400 px-2 py-1 text-xs">Rejected as non-causal: <Mono>{e.src}</Mono>. {e.note}</p>))}
              </div>
            ) : <p className="text-sm text-slate-500">No analysis yet. The engine runs when the merchant describes a settlement problem.</p>}
          </Card>

          <Card title="Ranked actions (including do nothing)">
            {report ? <div className="space-y-3"><RankedTable ranked={report.ranked} chosenId={report.chosen.id} /><WhyDrawer r={report} /></div>
              : <p className="text-sm text-slate-500">Nothing to rank yet.</p>}
          </Card>

          <Card title="Promise ledger"><LedgerPanel rows={c?.ledger ?? []} /></Card>
          <Card title="Decision records (SQLite)"><RecordsPanel decisions={c?.decisions ?? []} outcomes={c?.outcomes ?? []} followups={c?.followups ?? []} /></Card>

          {approval && c && can("approval.decide") && (
            <Card title="Analyst decision needed">
              <p className="mb-2 text-sm">{approval.label}: {approval.action.params.docs?.join(", ")} for {approval.action.params.dispute_id}.
                {c.memo && <> If nothing is done, {(c.memo.if_nothing_p_miss * 100).toFixed(0)}% chance the supplier payment is missed.</>}</p>
              <div className="flex gap-2">
                <Btn kind="good" onClick={() => act(() => api.caseDecide(approval.action.hash, true, sid))}>Approve</Btn>
                <Btn kind="danger" onClick={() => act(() => api.caseDecide(approval.action.hash, false, sid))}>Reject</Btn>
                <a className="self-center text-xs text-indigo-600 underline" href="#/approvals">Open full card (edit bundle)</a>
              </div>
            </Card>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="State ladder">
          <StateLadder plan={c?.plan ?? []} />
          {c?.resolution && (
            <p className="mt-3 border-t border-slate-100 pt-2 text-xs dark:border-slate-800">
              Case resolution check: <b className={c.resolution.state === "verified" ? "text-emerald-600" : ""}>{c.resolution.state}</b> · {c.resolution.reason}
              {evidence && evidence.state === "verified" && c.resolution.state !== "verified" && <span className="text-slate-500"> (evidence submitted is an action result, not a resolution)</span>}
            </p>)}
        </Card>
        <Card title={`Action log and audit (${audit.length} entries)`}
          right={can("audit.verify") && (
            <div className="flex gap-2">
              <Btn onClick={() => api.auditVerify(sid).then((v) => setVerify(v.memory.ok && v.file.ok ? `Audit chain verified (${v.memory.entries} entries, memory and encrypted file)` : `AUDIT CHAIN BROKEN: ${v.memory.reason ?? v.file.reason}`)).catch((e) => setErr(String(e)))}>Verify chain</Btn>
              {can("data.delete") && <Btn kind="danger" onClick={() => { if (confirm("Delete this case and its audit file? This cannot be undone.")) api.deleteCase(sid).then(() => { location.hash = "#/"; }).catch((e) => setErr(String(e))); }}>Delete case data</Btn>}
            </div>)}>
          {verify && <p className={`mb-2 text-xs font-medium ${verify.startsWith("Audit chain verified") ? "text-emerald-600" : "text-red-600"}`}>{verify}</p>}
          {can("audit.read") ? <ActionLog audit={audit} /> : <p className="text-sm text-slate-500">Audit detail is limited to analysts and admins.</p>}
        </Card>
      </div>
    </div>
  );
}
