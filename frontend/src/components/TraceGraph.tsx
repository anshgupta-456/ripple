import { GraphEdge, GraphNode } from "../api";

const W = 176, H = 50, GX = 52, GY = 22;

const KIND_LABEL: Record<string, string> = {
  evidence_gap: "root cause", review_pending: "waiting", dispute_lost: "dispute", risk_hold: "risk hold",
  settlement_funds: "funds", supplier_obligation: "obligation", kyc_status: "KYC", unknown: "unknown",
};

function layout(nodes: GraphNode[], edges: GraphEdge[]) {
  const layer: Record<string, number> = Object.fromEntries(nodes.map((n) => [n.id, 0]));
  for (let i = 0; i < nodes.length; i++) for (const e of edges) layer[e.dst] = Math.max(layer[e.dst] ?? 0, (layer[e.src] ?? 0) + 1);
  const cols: Record<number, GraphNode[]> = {};
  nodes.forEach((n) => (cols[layer[n.id]] ??= []).push(n));
  const maxRows = Math.max(...Object.values(cols).map((c) => c.length), 1);
  const pos: Record<string, { x: number; y: number }> = {};
  Object.entries(cols).forEach(([l, ns]) => {
    // decoys (soft-only sources) sit below the causal spine
    ns.sort((a, b) => Number(a.kind === "kyc_status") - Number(b.kind === "kyc_status") || a.id.localeCompare(b.id));
    ns.forEach((n, i) => {
      const offset = ((maxRows - ns.length) * (H + GY)) / 2;
      pos[n.id] = { x: 12 + Number(l) * (W + GX), y: 12 + offset + i * (H + GY) };
    });
  });
  const width = 24 + (Math.max(...Object.values(layer), 0) + 1) * (W + GX) - GX;
  const height = 24 + maxRows * (H + GY) - GY;
  return { pos, width, height };
}

export default function TraceGraph({ nodes, edges, path }: { nodes: GraphNode[]; edges: GraphEdge[]; path: string[] }) {
  if (!nodes.length) return <p className="text-sm text-slate-500">No graph yet.</p>;
  const { pos, width, height } = layout(nodes, edges);
  const onPath = new Set(path);
  const edgeStyle = (cls: string) =>
    cls === "confirmed_causal" ? { cls: "text-indigo-500", dash: undefined, label: "confirmed" }
      : cls === "obligation" ? { cls: "text-slate-500", dash: undefined, label: "obligation" }
        : cls === "soft" ? { cls: "text-amber-500", dash: "5 4", label: "non-causal" }
          : { cls: "text-red-400", dash: "2 4", label: "unknown" };
  return (
    <div className="overflow-x-auto">
      <svg width={width} height={height + 8} viewBox={`0 0 ${width} ${height + 8}`} role="img" aria-label="Cause and obligation graph">
        <defs>
          {["text-indigo-500", "text-slate-500", "text-amber-500", "text-red-400"].map((c) => (
            <marker key={c} id={`ah-${c}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0 0 L10 5 L0 10 z" className={`${c} fill-current`} />
            </marker>))}
        </defs>
        {edges.map((e, i) => {
          const a = pos[e.src], b = pos[e.dst];
          if (!a || !b) return null;
          const st = edgeStyle(e.edge_class);
          const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, mx = (x1 + x2) / 2;
          const hot = onPath.has(e.src) && onPath.has(e.dst);
          return (
            <g key={i} className={st.cls}>
              <path d={`M${x1} ${y1} C${mx} ${y1}, ${mx} ${y2}, ${x2 - 2} ${y2}`} fill="none" stroke="currentColor" strokeWidth={hot ? 2.6 : 1.6}
                strokeDasharray={st.dash} markerEnd={`url(#ah-${st.cls})`}>
                <title>{`${e.edge_class}: ${e.note}\nprovenance: ${e.provenance.join(", ") || "-"}`}</title>
              </path>
              <text x={mx} y={(y1 + y2) / 2 - 5} textAnchor="middle" className="fill-current text-[10px]">{st.label}</text>
            </g>
          );
        })}
        {nodes.map((n) => {
          const p = pos[n.id];
          const decoy = n.kind === "kyc_status" && edges.some((e) => e.src === n.id && e.decoy);
          const root = n.controllable && onPath.has(n.id) && n.kind === "evidence_gap";
          const stroke = decoy ? "stroke-amber-400" : root ? "stroke-emerald-500" : onPath.has(n.id) ? "stroke-indigo-500" : "stroke-slate-300 dark:stroke-slate-600";
          return (
            <g key={n.id}>
              <rect x={p.x} y={p.y} width={W} height={H} rx={8} className={`fill-white dark:fill-slate-900 ${stroke}`}
                strokeWidth={onPath.has(n.id) ? 2.4 : 1.4} strokeDasharray={decoy ? "5 4" : undefined} />
              <text x={p.x + 10} y={p.y + 16} className="fill-slate-500 text-[9px] uppercase tracking-wide">{decoy ? "decoy · not a cause" : KIND_LABEL[n.kind] ?? n.kind}</text>
              <text x={p.x + 10} y={p.y + 33} className="fill-slate-900 text-[11px] font-medium dark:fill-slate-100">
                {(n.label.length > 27 ? n.label.slice(0, 26) + "…" : n.label)}
              </text>
              <title>{`${n.id}${n.amount ? `\nRs ${n.amount.toLocaleString("en-IN")}` : ""}${n.due_at ? `\ndue ${n.due_at}` : ""}`}</title>
            </g>
          );
        })}
      </svg>
      <p className="mt-1 flex flex-wrap gap-x-4 text-[11px] text-slate-500">
        <span><span className="text-indigo-500">━</span> confirmed cause</span>
        <span><span className="text-slate-500">━</span> obligation dependency</span>
        <span><span className="text-amber-500">┅</span> correlated, ignored</span>
        <span>thick = traced path</span>
      </p>
    </div>
  );
}
