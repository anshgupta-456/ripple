// Hand-written for Phase 0/1. Replace with types from `npm run gen:types` (src/api/schema.d.ts) in Phase 2.
export interface WorldEvent {
  id: string;
  ts: string;
  source: string;
  type: string;
  payload: Record<string, unknown>;
  source_ref: string;
  freshness: string;
}

export interface SessionState {
  session_id: string;
  banner: string;
  scenario: {
    scenario_id: string;
    family: string;
    description: string;
    split: string;
    inbound: { text: string; language: string };
  };
  world: Record<string, any>;
}

export interface ScenarioInfo {
  family: string;
  split: string;
  description: string;
}

async function j<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

export interface RankedAction {
  id: string; label: string; steps: string[]; tier: number; permitted: boolean; feasible: boolean;
  reason: string; p_miss: number; expected_harm: number; cost: number; risk_penalty: number;
  score: number; decoy: boolean;
}

export interface Analysis {
  seed: number;
  n_samples: number;
  disposition: string;
  triggers: string[];
  conflicts: string[];
  stability: number;
  obligation_at_risk: boolean;
  p_miss_do_nothing: number;
  due_hours: number | null;
  graph: { version: number; digest: string };
  trace: {
    status: string; path: string[]; root_cause: string | null;
    edges: { src: string; dst: string; edge_class: string; note: string }[];
    rejected_edges: { src: string; dst: string; edge_class: string; note: string }[];
  };
  ranked: RankedAction[];
  chosen: RankedAction;
}

export const api = {
  analyze: () => j<Analysis>("/api/session/analyze", {}),
  scenarios: () => j<ScenarioInfo[]>("/api/scenarios"),
  state: () => j<SessionState>("/api/session/state"),
  reset: (family: string) => j<SessionState>("/api/session/reset", { family }),
  advance: (hours: number) => j<SessionState>("/api/session/advance", { hours }),
  tool: (tool: string, params: object, idem_key: string) =>
    j<{ result: unknown }>("/api/dev/tool", { tool, params, idem_key }),
};
