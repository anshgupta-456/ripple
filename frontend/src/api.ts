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

export interface ProposedAction { type: string; hash: string; tier: number; params: Record<string, unknown> }
export interface Decision { allowed: boolean; code: string; reason: string; tier: number | null; needs: string | null }
export interface ExecResult {
  action_hash: string; action_type: string; outcome: string; firewall: Decision; ladder: string[];
  reconciled: boolean; retried: boolean; verified: boolean;
  verification: { state: string; reason: string } | null;
}
export interface AuditEntry { seq: number; ts: string; kind: string; outcome: string; code: string; action_hash: string; data: Record<string, unknown> }
export interface AdversarialSummary { action_cases: number; unsafe_actions: number; screening_cases: number; screening_misses: string[]; screening_false_positives: string[] }

export const api = {
  analyze: () => j<Analysis>("/api/session/analyze", {}),
  scenarios: () => j<ScenarioInfo[]>("/api/scenarios"),
  state: () => j<SessionState>("/api/session/state"),
  reset: (family: string) => j<SessionState>("/api/session/reset", { family }),
  advance: (hours: number) => j<SessionState>("/api/session/advance", { hours }),
  propose: (type: string, params: object = {}) =>
    j<{ action: ProposedAction; decision: Decision }>("/api/actions/propose", { type, params }),
  approve: (action_hash: string, approver: string, role: string) =>
    j<unknown>("/api/approvals/issue", { action_hash, approver, role }),
  consent: (action_hash: string) => j<unknown>("/api/consents/issue", { action_hash }),
  execute: (action_hash: string) => j<ExecResult>("/api/actions/execute", { action_hash }),
  audit: () => j<AuditEntry[]>("/api/audit"),
  adversarial: () => j<AdversarialSummary>("/api/adversarial/run", {}),
};
