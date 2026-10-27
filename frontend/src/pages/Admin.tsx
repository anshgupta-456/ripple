import { useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Btn, Card, ErrorNote, Mono, fmtTime, usePolling } from "../components/ui";

type Log = Awaited<ReturnType<typeof api.securityLog>>;

export default function Admin() {
  const { me, refresh } = useAuth();
  const [kill, setKill] = useState<{ global: boolean; users: string[] } | null>(null);
  const [log, setLog] = useState<Log | null>(null);
  const [target, setTarget] = useState("dev:priya");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const load = () => {
    api.killState().then(setKill).catch((e) => setErr(String(e)));
    api.securityLog().then(setLog).catch(() => {});
  };
  usePolling(load, 5000);

  const act = async (fn: () => Promise<unknown>, done?: string) => {
    try { setErr(""); await fn(); if (done) setMsg(done); load(); refresh(); } catch (e) { setErr(String(e)); }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline gap-2">
        <h1 className="text-xl font-semibold">Admin and governance</h1>
        <span className="text-sm text-slate-500">Kill switch, security audit trail, retention, and your own access.</span>
      </div>
      <ErrorNote text={err} />
      {msg && <p className="rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200">{msg}</p>}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Kill switch">
          <p className="text-sm text-slate-600 dark:text-slate-300">Stops the agent from acting: the API refuses agent actions with 503, the orchestrator does nothing, and the firewall denies every call (even reads) as a backstop. People can still read dashboards. State survives restarts.</p>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <span className={`rounded-full px-3 py-1 text-sm font-semibold ${kill?.global ? "bg-red-600 text-white" : "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200"}`}>
              {kill?.global ? "AGENT DISABLED (global)" : "Agent enabled"}</span>
            <Btn kind={kill?.global ? "good" : "danger"} onClick={() => act(() => api.killSet("global", !kill?.global), kill?.global ? "Agent re-enabled." : "Agent disabled for everyone.")}>
              {kill?.global ? "Re-enable agent" : "Disable agent globally"}</Btn>
          </div>
          <div className="mt-4 border-t border-slate-100 pt-3 dark:border-slate-800">
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Per user</p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <input className="rounded-md border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700" value={target} onChange={(e) => setTarget(e.target.value)} aria-label="User subject" />
              <Btn onClick={() => act(() => api.killSet("user", true, target), `Disabled ${target}.`)}>Disable user</Btn>
            </div>
            <ul className="mt-2 space-y-1 text-sm">
              {kill?.users.map((u) => (
                <li key={u} className="flex items-center gap-2"><Mono>{u}</Mono><Btn onClick={() => act(() => api.killSet("user", false, u), `Re-enabled ${u}.`)}>Re-enable</Btn></li>))}
              {!kill?.users.length && <li className="text-slate-500">No users disabled.</li>}
            </ul>
          </div>
        </Card>

        <Card title="Your access">
          {me && (
            <div className="space-y-2 text-sm">
              <p><b>{me.name}</b> <Mono className="text-slate-500">{me.sub}</Mono></p>
              <p>Roles: <b>{me.roles.join(", ")}</b> · tenant <Mono>{me.tenant}</Mono></p>
              <p>Token expires {fmtTime(new Date(me.expires_at * 1000).toISOString())} (short-lived; no long-lived keys are used).</p>
              <p className="text-xs text-slate-500">Permissions: {me.perms.join(", ")}</p>
              <p className="text-xs text-slate-500">Approving risk actions needs the analyst role. Admins cannot approve (separation of duties). Roles come only from the verified token, never from a request body.</p>
            </div>)}
        </Card>

        <Card title="Data retention and deletion">
          <p className="text-sm text-slate-600 dark:text-slate-300">Audit files are encrypted at rest and expire after the retention period. Individual cases can be deleted from the case page (removes the case and its audit file).</p>
          <div className="mt-3"><Btn onClick={() => act(async () => { const r = await api.purge(); setMsg(`Purged ${r.deleted_files} expired file(s) (retention ${r.retention_days} days).`); })}>Run retention purge now</Btn></div>
        </Card>

        <Card title="Security audit trail" right={log && <span className={`text-xs font-semibold ${log.verify.ok ? "text-emerald-600" : "text-red-600"}`}>{log.verify.ok ? `chain verified (${log.verify.entries} entries)` : "CHAIN BROKEN"}</span>}>
          <div className="max-h-72 overflow-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-white text-left text-slate-500 dark:bg-slate-900"><tr><th className="py-1 pr-2">When</th><th className="pr-2">Event</th><th className="pr-2">Outcome</th><th>Who</th></tr></thead>
              <tbody>
                {log?.entries.slice().reverse().map((e) => (
                  <tr key={e.seq} className="border-t border-slate-100 dark:border-slate-800">
                    <td className="py-1 pr-2 whitespace-nowrap text-slate-500">{e.ts.slice(11, 19)}</td>
                    <td className="pr-2">{e.kind.replace("security.", "")}{e.data.perm ? ` (${e.data.perm})` : ""}</td>
                    <td className={`pr-2 ${e.outcome === "deny" ? "text-red-600" : ""}`}>{e.outcome || e.code}</td>
                    <td><Mono>{String(e.data.user ?? e.actor?.sub ?? "-")}</Mono></td>
                  </tr>))}
                {!log?.entries.length && <tr><td colSpan={4} className="py-3 text-slate-500">No events yet.</td></tr>}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-xs text-slate-500">Logins, authorization denials, impersonation, kill-switch changes and deletions. Entries are HMAC-chained; editing or removing one breaks verification.</p>
        </Card>
      </div>
    </div>
  );
}
