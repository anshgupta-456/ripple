import { useEffect, useState } from "react";
import { api, PortfolioResult } from "../api";
import { Card, ErrorNote } from "../components/ui";

export default function Portfolio() {
  const [p, setP] = useState<PortfolioResult | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => { api.portfolio().then(setP).catch((e) => setErr(String(e))); }, []);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline gap-2">
        <h1 className="text-xl font-semibold">Portfolio view</h1>
        <span className="text-sm text-slate-500">Which upstream fix would remove the most cascades across many merchants?</span>
      </div>
      <p role="note" className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200">
        Illustrative. {p?.n ?? 200} synthetic merchants with a <b>planted</b> cause distribution, each run through the real decision engine. This shows what the view does, not what a real merchant base looks like.
      </p>
      <ErrorNote text={err} />
      {!p && !err && <p className="text-sm text-slate-500">Running the engine over the merchants (about half a minute the first time)…</p>}
      {p && (<>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {[["Merchants simulated", p.n], ["With a supplier payment at risk", p.totals.at_risk], ["Expected cascades prevented by the chosen fixes", p.totals.cascades_prevented]].map(([l, v]) => (
            <Card key={String(l)}><p className="text-3xl font-semibold">{v}</p><p className="text-xs text-slate-500">{l}</p></Card>))}
        </div>
        <Card title="Upstream fixes, ranked by cascades prevented per unit of effort">
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase text-slate-500"><tr><th className="pr-3">Upstream fix</th><th className="pr-3">Traced root cause</th><th className="pr-3">Merchants</th><th className="pr-3">At risk</th><th className="pr-3">Prevented</th><th className="pr-3">Effort</th><th>Per effort</th></tr></thead>
            <tbody>{p.ranked.map((r, i) => (
              <tr key={i} className="border-t border-slate-100 dark:border-slate-800"><td className="py-1.5 pr-3 font-medium">{r.upstream_fix}</td><td className="pr-3">{r.root_cause}</td><td className="pr-3">{r.merchants}</td>
                <td className="pr-3">{r.at_risk}</td><td className="pr-3">{r.cascades_prevented}</td><td className="pr-3">{r.effort}</td><td className="font-semibold">{r.prevented_per_effort}</td></tr>))}</tbody>
          </table>
          <p className="mt-2 text-xs text-slate-500">Effort is relative: 1 for an action the agent takes alone, 2 with merchant consent, 3 with human approval. “Prevented” is the drop in the probability of a missed supplier payment, summed over merchants.</p>
        </Card>
        <Card title="By scenario family (planted share and what the engine traced)">
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase text-slate-500"><tr><th className="pr-3">Family</th><th className="pr-3">Planted share</th><th className="pr-3">Merchants</th><th className="pr-3">At risk</th><th className="pr-3">Escalated</th><th>Traced cause</th></tr></thead>
            <tbody>{Object.entries(p.by_family).map(([f, v]) => (
              <tr key={f} className="border-t border-slate-100 dark:border-slate-800"><td className="py-1.5 pr-3">{f.replaceAll("_", " ")}</td><td className="pr-3">{((p.planted_distribution[f] ?? 0) * 100).toFixed(0)}%</td><td className="pr-3">{v.merchants}</td>
                <td className="pr-3">{v.at_risk}</td><td className="pr-3">{v.escalate}</td><td>{v.traced_cause}</td></tr>))}</tbody>
          </table>
        </Card>
      </>)}
    </div>
  );
}
