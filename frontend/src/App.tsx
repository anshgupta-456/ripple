import { useCallback, useEffect, useRef, useState } from "react";
import { api, ScenarioInfo, SessionState, WorldEvent } from "./api";

const DOCS = ["invoice", "delivery_proof", "customer_comm"];

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h2>
      {children}
    </section>
  );
}

function Kv({ data }: { data: Record<string, unknown> | null }) {
  if (!data) return <p className="text-sm text-slate-500">none</p>;
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
      {Object.entries(data).map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-slate-500">{k}</dt>
          <dd className="break-all font-mono">{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

export default function App() {
  const [scenarios, setScenarios] = useState<ScenarioInfo[]>([]);
  const [state, setState] = useState<SessionState | null>(null);
  const [events, setEvents] = useState<WorldEvent[]>([]);
  const [family, setFamily] = useState("missing_evidence_flagship");
  const [log, setLog] = useState("");
  const [live, setLive] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  const refresh = useCallback(async () => setState(await api.state()), []);

  useEffect(() => {
    api.scenarios().then(setScenarios).catch((e) => setLog(String(e)));
    refresh().catch((e) => setLog(String(e)));
    const es = new EventSource("/api/session/stream");
    esRef.current = es;
    es.onopen = () => setLive(true);
    es.onerror = () => setLive(false);
    es.addEventListener("reset", () => setEvents([]));
    es.addEventListener("world_event", (m) => {
      setEvents((prev) => [...prev, JSON.parse((m as MessageEvent).data)]);
      refresh().catch(() => {});
    });
    return () => es.close();
  }, [refresh]);

  const run = async (fn: () => Promise<unknown>) => {
    try {
      setLog("");
      await fn();
      await refresh();
    } catch (e) {
      setLog(String(e));
    }
  };

  const w = state?.world;
  const disputeId = w?.dispute?.dispute_id as string | undefined;

  return (
    <div className="mx-auto max-w-6xl p-4">
      <div className="mb-4 rounded-md bg-amber-100 px-3 py-2 text-center text-xs font-semibold text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">
        {state?.banner ?? "SYNTHETIC DATA"}
      </div>
      <header className="mb-4 flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-bold">RIPPLE <span className="text-sm font-normal text-slate-500">world inspector (Phase 1)</span></h1>
        <span className={`rounded px-2 py-0.5 text-xs ${live ? "bg-emerald-100 text-emerald-800" : "bg-red-100 text-red-800"}`}>
          {live ? "live stream" : "disconnected"}
        </span>
        <div className="ml-auto flex flex-wrap gap-2">
          <select className="rounded border bg-transparent px-2 py-1 text-sm" value={family} onChange={(e) => setFamily(e.target.value)}>
            {scenarios.map((s) => <option key={s.family} value={s.family}>{s.family}{s.split === "held_out" ? " (held-out)" : ""}</option>)}
          </select>
          <button className="rounded bg-slate-900 px-3 py-1 text-sm text-white dark:bg-slate-100 dark:text-slate-900"
            onClick={() => run(async () => { setEvents([]); await api.reset(family); })}>Reset</button>
          {[6, 20, 26].map((h) => (
            <button key={h} className="rounded border px-3 py-1 text-sm" onClick={() => run(() => api.advance(h))}>+{h}h</button>
          ))}
        </div>
      </header>

      {log && <pre className="mb-3 rounded bg-red-50 p-2 text-xs text-red-800">{log}</pre>}

      {state && (
        <>
          <Card title={`Merchant message (${state.scenario.inbound.language})`}>
            <p className="text-lg">“{state.scenario.inbound.text}”</p>
            <p className="mt-1 text-xs text-slate-500">{state.scenario.description}</p>
            <p className="mt-1 text-xs text-slate-500">Sim time: {w?.now}</p>
          </Card>

          <div className="mt-4 grid gap-4 md:grid-cols-2">
            <Card title="Settlement"><Kv data={w?.settlement} /></Card>
            <Card title="Dispute"><Kv data={w?.dispute} /></Card>
            <Card title="KYC"><Kv data={w?.kyc} /></Card>
            <Card title="Supplier obligation"><Kv data={w?.supplier} /></Card>
          </div>

          <div className="mt-4 grid gap-4 md:grid-cols-2">
            <Card title="Dev: manual tool calls (firewall arrives in Phase 3)">
              <div className="flex flex-wrap gap-2 text-sm">
                {disputeId && (
                  <button className="rounded border px-2 py-1"
                    onClick={() => run(() => api.tool("submit_dispute_evidence", { dispute_id: disputeId, docs: DOCS }, "ui-evidence-1"))}>
                    Submit full evidence
                  </button>
                )}
                {disputeId && (
                  <button className="rounded border px-2 py-1"
                    onClick={() => run(() => api.tool("submit_dispute_evidence", { dispute_id: disputeId, docs: ["invoice"] }, `ui-partial-${Date.now()}`))}>
                    Submit partial evidence
                  </button>
                )}
                <button className="rounded border px-2 py-1"
                  onClick={() => run(() => api.tool("send_kyc_reminder", {}, `ui-kyc-${Date.now()}`))}>
                  KYC reminder
                </button>
                <button className="rounded border border-red-300 px-2 py-1 text-red-700"
                  onClick={() => run(() => api.tool("release_risk_hold", {}, "ui-attack"))}>
                  Try release_risk_hold (expect 404)
                </button>
              </div>
            </Card>
            <Card title={`Event stream (${events.length})`}>
              <ul className="max-h-72 space-y-1 overflow-auto text-xs">
                {events.map((e) => (
                  <li key={e.id} className="font-mono">
                    <span className="text-slate-500">{e.ts.slice(5, 16)}</span>{" "}
                    <b>{e.source}.{e.type}</b>{" "}
                    <span className="text-slate-500">{e.source_ref}</span>
                  </li>
                ))}
                {events.length === 0 && <li className="text-slate-500">no events yet. Submit evidence, then advance time.</li>}
              </ul>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
