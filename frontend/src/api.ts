// Hand-written client. Types mirror backend responses (also see src/api/schema.d.ts from `npm run gen:types`).
export interface RankedAction {
  id: string; label: string; steps: string[]; tier: number; permitted: boolean; feasible: boolean;
  reason: string; p_miss: number; expected_harm: number; cost: number; risk_penalty: number;
  score: number; decoy: boolean; release_p50_h: number | null; release_p90_h: number | null;
}
export interface GraphNode { id: string; kind: string; label: string; controllable?: boolean; amount?: number; due_at?: string }
export interface GraphEdge { src: string; dst: string; edge_class: string; provenance: string[]; note: string; decoy: boolean }
export interface Report {
  seed: number; n_samples: number; disposition: string; triggers: string[]; conflicts: string[]; stability: number;
  obligation_at_risk: boolean; p_miss_do_nothing: number; due_hours: number | null;
  params: Record<string, any>;
  graph: { version: number; digest: string; nodes: GraphNode[]; edges: GraphEdge[] };
  trace: { status: string; path: string[]; root_cause: string | null; root_kind: string | null;
           edges: { src: string; dst: string; edge_class: string; note: string; provenance: string[] }[];
           rejected_edges: { src: string; dst: string; edge_class: string; note: string; provenance: string[] }[] };
  ranked: RankedAction[]; chosen: RankedAction;
}
export interface CaseMessage { role: string; text: string; ts: string; kind: string }
export interface CaseStep { ts: string; label: string; detail: string }
export interface PlannedAction {
  action: { type: string; hash: string; idempotency_key: string; params: Record<string, any>; expires_at: string | null };
  label: string; tier: number; state: string; note: string; notice_text: string | null; ladder: string[];
}
export interface Memo {
  merchant_ask: string; summary: string; minutes_saved: number; disposition: string; stability: number; seed: number;
  graph_version: number; if_nothing_p_miss: number; hours_to_deadline: number | null; triggers: string[];
  deadline: string | null;
  facts: { label: string; value: string; source_ref: string }[];
  cause_path: string[];
  rejected_edges: { src: string; dst: string; edge_class: string; note: string }[];
  options: { id: string; label: string; tier: number; p_miss: number; score: number; chosen: boolean; note: string }[];
  evidence: { dispute_id: string; docs: string[]; missing: string[]; summary: string[]; provenance_note: string } | null;
}
export interface AuditEntry { seq: number; ts: string; kind: string; outcome: string; code: string; action_hash: string; data: Record<string, any> }
export interface CaseView {
  sid: string; case_id: string; status: string; language: string; intent: string; closed: boolean; scenario_family: string;
  merchant_name: string; sim_now: string; unsafe: { tool: string; kind: string }[];
  messages: CaseMessage[]; steps: CaseStep[]; plan: PlannedAction[]; memo: Memo | null; report: Report | null;
  resolution: { state: string; reason: string } | null; pending: string[];
  bundle: { docs: string[]; dispute_id: string } | null;
}
export interface CaseSummary {
  sid: string; case_id: string; merchant: string; family: string; intent: string; language: string; status: string;
  closed: boolean; waiting_on: string | null; tier_max: number; hours_to_deadline: number | null; at_risk: boolean;
  p_miss: number | null; last_agent_message: string; n_messages: number; sim_now: string; unsafe: number; description: string;
}
export interface Kpis {
  total: number; auto_resolved: number; verified: number; awaiting_analyst: number; awaiting_merchant: number;
  escalated: number; open: number; at_risk: number; unsafe_actions: number; modeled_minutes_saved: number;
  autonomous_rate: { k: number; n: number; label: string };
}
export interface PendingApproval {
  sid: string; case_id: string; merchant: string; family: string; label: string; tier: number; sim_now: string;
  hours_to_deadline: number | null; action: PlannedAction["action"]; memo: Memo | null;
  bundle: { docs: string[]; dispute_id: string; missing: string[]; summary: string[]; provenance_note: string } | null;
}
export interface ScenarioInfo { family: string; split: string; description: string }
export interface SessionState {
  session_id: string; banner: string;
  scenario: { scenario_id: string; family: string; description: string; split: string; inbound: { text: string; language: string } };
  world: Record<string, any>;
}
export interface Rate { k: number; n: number; rate: number | null; ci95: number[] | null }
export interface SysSummary {
  label: string; n_runs: number;
  autonomous_resolution_rate: Rate; verified_resolution_rate: Rate; escalation_precision: Rate; escalation_recall: Rate;
  false_escalation_rate: Rate; false_resolved: Rate; injection_complied: Rate; causal_trace_precision: Rate;
  do_nothing_correctness: Rate; dedupe_ok: Rate; no_blind_write_retry: Rate;
  unsafe_actions: { total: number; cases: number; by_kind: Record<string, number> };
  missed_obligations: { count: number; avoidable: number; unavoidable: number; of: number; weighted_avoidable: number };
  time_to_verified_resolution_h: { median: number | null; p90: number | null; n: number };
  cost_per_case_inr: { mean: number | null; manual_baseline: number };
  repeat_contacts: number; blind_retries: number; false_causal_claims: number;
  human_minutes_saved_per_escalated_case: number; decision_stability_mean: number | null;
  repeat_contacts_avoided_vs_A?: number;
}
export interface FamilyCell { verified: number; handoff: number; false_resolved: number; unsafe: number; missed: number; n: number }
export interface BenchResults {
  meta: { label: string; scored: boolean; config_hash: string; policy_hash: string; git_commit: string; git_dirty: boolean;
          git_tags_at_head: string[]; n_runs: number; n_scenarios: number; notes: string[]; held_out_families: string[];
          systems: Record<string, string>; config_version: string; seeds: number[] };
  summary: { all: Record<string, SysSummary>; held_out: Record<string, SysSummary>; tuning: Record<string, SysSummary>;
             by_family: Record<string, Record<string, FamilyCell>> };
  promise_eval: { n: number; promise_detection_accuracy: number; weekday_accuracy: number; misses: string[] };
  adversarial: { action_cases: number; unsafe_actions: number; screening_cases: number };
}
export interface PublicConfig {
  version: string; banner: string; cost_model: Record<string, number>; harm_weights: Record<string, number>;
  policy: Record<string, number>; held_out_families: string[]; engine: Record<string, any>;
}
export interface LabStep { t: string; actor: string; text: string; flag: string }
export interface LabSystem {
  system: string; label: string; headline: string; closed: boolean; claimed_resolved: boolean; verified_close: boolean;
  false_resolved: boolean; unsafe: { tool: string; kind: string }[]; unsafe_count: number; handoff: boolean;
  blind_retries: number; injection_complied: boolean; blocked_attempts: number; false_causal_claim: boolean;
  missed_obligation: boolean; final: { settlement: string; dispute: string | null; supplier_notices: number; kyc_reminders: number };
  replies: string[]; steps: LabStep[]; replay?: { blocked?: boolean; code?: string; note?: string } | null;
}
export interface LabResult { preset: string; family: string; description: string; message: string; custom_message: boolean; systems: Record<string, LabSystem> }
export interface LabPresets { presets: { id: string; family: string; description: string }[]; attack_texts: string[] }
export interface AdversarialSummary { action_cases: number; unsafe_actions: number; screening_cases: number; screening_misses: string[]; screening_false_positives: string[] }

async function j<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = await res.text();
    try { const d = JSON.parse(msg); msg = typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail ?? d); } catch { /* keep text */ }
    throw new Error(`${res.status}: ${msg}`);
  }
  return res.json() as Promise<T>;
}
const q = (path: string, sid?: string) => (sid ? `${path}${path.includes("?") ? "&" : "?"}sid=${encodeURIComponent(sid)}` : path);
const ANALYST = { approver: "Priya (risk analyst)", role: "analyst" };

export const api = {
  health: () => j<{ ok: boolean }>("/api/health"),
  config: () => j<PublicConfig>("/api/config/public"),
  scenarios: () => j<ScenarioInfo[]>("/api/scenarios"),
  // portfolio
  cases: () => j<CaseSummary[]>("/api/cases"),
  kpis: () => j<Kpis>("/api/console/kpis"),
  seed: () => j<CaseSummary[]>("/api/cases/seed", {}),
  newCase: (family: string, text?: string) => j<CaseSummary>("/api/cases/new", { family, text: text || null }),
  advanceAll: (hours: number) => j<CaseSummary[]>("/api/cases/advance", { hours }),
  pending: () => j<PendingApproval[]>("/api/approvals/pending"),
  // one case
  caseGet: (sid?: string) => j<CaseView>(q("/api/case", sid)),
  caseMessage: (text: string, sid?: string) => j<CaseView>(q("/api/case/message", sid), { text }),
  caseDecide: (action_hash: string, approve: boolean, sid?: string) =>
    j<CaseView>(q("/api/case/decide", sid), { action_hash, approve, ...ANALYST }),
  caseEdit: (action_hash: string, docs: string[], sid?: string) => j<CaseView>(q("/api/case/edit", sid), { action_hash, docs }),
  caseConsent: (action_hash: string, accept: boolean, sid?: string) => j<CaseView>(q("/api/case/consent", sid), { action_hash, accept }),
  advance: (hours: number, sid?: string) => j<SessionState>(q("/api/session/advance", sid), { hours }),
  state: (sid?: string) => j<SessionState>(q("/api/session/state", sid)),
  events: (sid?: string) => j<{ id: string; ts: string; source: string; type: string; source_ref: string }[]>(q("/api/session/events", sid)),
  audit: (sid?: string) => j<AuditEntry[]>(q("/api/audit", sid)),
  // benchmark + safety
  bench: () => j<BenchResults>("/api/bench/results"),
  adversarial: () => j<AdversarialSummary>("/api/adversarial/run", {}),
  // lab
  labPresets: () => j<LabPresets>("/api/lab/presets"),
  labRun: (preset: string, message?: string, replay = false) => j<LabResult>("/api/lab/run", { preset, message: message || null, replay }),
};
