import { useState } from "react";
import { api, CaseSummary, Kpis, ScenarioInfo } from "../api";
import { useAuth } from "../auth";
import { Bar, Btn, Card, ErrorNote, StatusChip, Tile, hoursLabel, usePolling } from "../components/ui";

type Filter = "all" | "person" | "auto" | "escalated" | "verified";
const FILTERS: [Filter, string][] = [["all", "All"], ["person", "Needs a person"], ["auto", "Auto-resolved"], ["escalated", "Escalated"], ["verified", "Verified"]];

const waitLabel = (w: string | null) => ({ analyst: "Analyst", merchant: "Merchant", source: "Source system" } as Record<string, string>)[w ?? ""] ?? "-";

export default function Console() {
  const { can } = useAuth();
  const demo = can("demo.control");
  const [rows, setRows] = useState<CaseSummary[]>([]);
  const [k, setK] = useState<Kpis | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [families, setFamilies] = useState<ScenarioInfo[]>([]);
  const [family, setFamily] = useState("missing_evidence_flagship");
  const [text, setText] = useState("");

  const load = () => {
    Promise.all([api.cases(), api.kpis()]).then(([r, kp]) => { setRows(r); setK(kp); }).catch((e) => setErr(String(e)));
  };
  usePolling(load, 3000);
  usePolling(() => { if (!families.length) api.scenarios().then(setFamilies).catch(() => {}); }, 60000);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { setErr(""); await fn(); load(); } catch (e) { setErr(String(e)); } finally { setBusy(false); }
  };

  const shown = rows.filter((r) =>
    filter === "all" ? true : filter === "person" ? r.waiting_on === "analyst" || r.waiting_on === "merchant"
      : filter === "auto" ? r.status === "auto_resolved" : filter === "escalated" ? r.status === "escalated" : r.status === "verified");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold">Ops Console</h1>
        <span className="text-sm text-slate-500">Every case the agent is handling, and what each one is waiting on.</span>
        {demo && <div className="ml-auto flex flex-wrap gap-2">
          <Btn kind="primary" disabled={busy} onClick={() => act(() => api.seed())}>Load demo portfolio</Btn>
          <Btn disabled={busy || !rows.length} onClick={() => act(() => api.advanceAll(6))}>Advance all +6h</Btn>
          <Btn disabled={busy || !rows.length} onClick={() => act(() => api.advanceAll(26))}>+26h</Btn>
        </div>}
      </div>
      <ErrorNote text={err} />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
        <Tile label="Autonomous" value={k ? `${k.autonomous_rate.k}/${k.autonomous_rate.n}` : "-"} sub="no person needed" tone="good" />
        <Tile label="Needs analyst" value={k?.awaiting_analyst ?? "-"} tone={k && k.awaiting_analyst ? "warn" : ""} sub="one-tap approvals" />
        <Tile label="Needs merchant" value={k?.awaiting_merchant ?? "-"} sub="consent pending" />
        <Tile label="Verified" value={k?.verified ?? "-"} sub="proven at the source" tone="good" />
        <Tile label="Escalated" value={k?.escalated ?? "-"} sub="handed to humans" />
        <Tile label="At risk" value={k?.at_risk ?? "-"} sub="deadline threatened" tone={k && k.at_risk ? "warn" : ""} />
        <Tile label="Unsafe actions" value={k?.unsafe_actions ?? "-"} sub="independent audit" tone={k ? (k.unsafe_actions === 0 ? "good" : "bad") : ""} />
      </div>

      {demo && <Card title="Simulate an incoming merchant message">
        <form className="flex flex-wrap items-start gap-2" onSubmit={(e) => { e.preventDefault(); act(() => api.newCase(family, text)); }}>
          <select className="rounded-md border border-slate-300 bg-transparent px-2 py-1.5 text-sm dark:border-slate-700" value={family} onChange={(e) => setFamily(e.target.value)}>
            {families.map((f) => <option key={f.family} value={f.family}>{f.family.replaceAll("_", " ")}</option>)}
            {!families.length && <option value="missing_evidence_flagship">missing evidence flagship</option>}
          </select>
          <input className="min-w-[18rem] flex-1 rounded-md border border-slate-300 bg-transparent px-2 py-1.5 text-sm dark:border-slate-700" value={text}
            placeholder="Merchant message (leave empty to use the scenario's own message)" onChange={(e) => setText(e.target.value)} />
          <Btn kind="primary" disabled={busy}>Send</Btn>
        </form>
        <p className="mt-2 text-xs text-slate-500">Creates a new case in a fresh synthetic world and runs the agent on it.</p>
      </Card>}

      <Card title={`Case queue (${shown.length})`}
        right={<div className="flex flex-wrap gap-1">{FILTERS.map(([f, label]) => (
          <button key={f} onClick={() => setFilter(f)}
            className={`rounded-full border px-2.5 py-0.5 text-xs ${filter === f ? "border-indigo-600 bg-indigo-600 text-white" : "border-slate-300 dark:border-slate-700"}`}>{label}</button>))}</div>}>
        {rows.length === 0 ? (
          <p className="py-8 text-center text-sm text-slate-500">No cases yet. {demo ? "Load the demo portfolio, or simulate a merchant message above." : "An admin can load the demo portfolio."}</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[56rem] text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr><th className="py-2 pr-3">Merchant</th><th className="pr-3">Case</th><th className="pr-3">Status</th><th className="pr-3">Waiting on</th>
                  <th className="pr-3">Deadline</th><th className="pr-3">P(miss)</th><th>Last agent message</th></tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={r.sid} onClick={() => { location.hash = `#/case/${r.sid}`; }}
                    className="cursor-pointer border-t border-slate-100 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-800/50">
                    <td className="py-2 pr-3 font-medium">{r.merchant}</td>
                    <td className="pr-3"><span className="text-slate-700 dark:text-slate-200">{r.family.replaceAll("_", " ")}</span>
                      <span className="block text-xs text-slate-500">{r.intent.replaceAll("_", " ") || "-"} · {r.language}</span></td>
                    <td className="pr-3"><StatusChip status={r.status} /></td>
                    <td className="pr-3">{waitLabel(r.waiting_on)}{r.waiting_on === "analyst" && <span className="ml-1 text-xs text-slate-500">tier {r.tier_max}</span>}</td>
                    <td className={`pr-3 tabular-nums ${r.hours_to_deadline != null && r.hours_to_deadline < 12 ? "font-semibold text-red-600" : ""}`}>{hoursLabel(r.hours_to_deadline)}</td>
                    <td className="pr-3">{r.p_miss != null ? <Bar value={r.p_miss} /> : <span className="text-slate-400">-</span>}</td>
                    <td className="max-w-xs truncate text-xs text-slate-500" title={r.last_agent_message}>{r.last_agent_message || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <p className="text-xs text-slate-500">Autonomous = auto-resolved ÷ (auto-resolved + cases that needed a person). Minutes saved and costs are modeled, not measured. {k && `Modeled analyst time saved so far: ${k.modeled_minutes_saved} min.`}</p>
    </div>
  );
}
