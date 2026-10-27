import { useState } from "react";
import { api, SessionState } from "../api";
import CaseWorkspace from "../components/CaseWorkspace";
import { Btn, Card, ErrorNote, usePolling } from "../components/ui";

export default function CasePage({ sid }: { sid: string }) {
  const [st, setSt] = useState<SessionState | null>(null);
  const [key, setKey] = useState(0);
  const [err, setErr] = useState("");
  const load = () => api.state(sid).then(setSt).catch((e) => setErr(String(e)));
  usePolling(load, 4000, [sid]);
  const bump = () => { load(); setKey((k) => k + 1); };
  const advance = (h: number) => api.advance(h, sid).then(bump).catch((e) => setErr(String(e)));

  if (err && !st) return <Card><p className="text-sm">{err}</p><a className="text-indigo-600 underline" href="#/">Back to the Ops Console</a></Card>;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <a className="text-sm text-indigo-600 underline" href="#/">← Ops Console</a>
        <h1 className="text-xl font-semibold">{st ? st.world.merchant.name : "Case"}</h1>
        <span className="text-xs text-slate-500">{st?.scenario.description}</span>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs text-slate-500">Simulated time: {st?.world.now.slice(0, 16).replace("T", " ")}</span>
          {[6, 20, 26].map((h) => <Btn key={h} onClick={() => advance(h)}>+{h}h</Btn>)}
        </div>
      </div>
      <ErrorNote text={err} />
      {st && <CaseWorkspace sid={sid} inbound={st.scenario.inbound.text} refreshKey={key} onChange={bump} />}
    </div>
  );
}
