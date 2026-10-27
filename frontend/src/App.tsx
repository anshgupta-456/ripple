import { useEffect, useState } from "react";
import { api } from "./api";
import { LoginScreen, useAuth } from "./auth";
import { usePolling } from "./components/ui";
import Admin from "./pages/Admin";
import Approvals from "./pages/Approvals";
import CasePage from "./pages/CasePage";
import Console from "./pages/Console";
import Lab from "./pages/Lab";
import Scoreboard from "./pages/Scoreboard";

function useHash(): string {
  const [h, setH] = useState(location.hash || "#/");
  useEffect(() => {
    const f = () => setH(location.hash || "#/");
    window.addEventListener("hashchange", f);
    return () => window.removeEventListener("hashchange", f);
  }, []);
  return h;
}

// [href, label, permission required to see it]
const NAV: [string, string, string][] = [
  ["#/", "Ops Console", "console.read"], ["#/approvals", "Approvals", "approval.read"], ["#/scoreboard", "Scoreboard", "bench.read"],
  ["#/lab", "Lab", "lab.run"], ["#/admin", "Admin", "killswitch.set"],
];

export default function App() {
  const hash = useHash();
  const { me, cfg, can, login, logout } = useAuth();
  const [pending, setPending] = useState(0);
  const [personas, setPersonas] = useState<{ id: string; name: string }[]>([]);
  usePolling(() => { if (me && can("approval.read")) api.pending().then((p) => setPending(p.filter((x) => x.can_decide).length)).catch(() => {}); }, 5000, [me?.sub]);
  useEffect(() => { if (me && cfg?.auth.dev_login) api.personas().then(setPersonas).catch(() => {}); }, [me, cfg]);

  const banner = cfg?.banner ?? "SYNTHETIC DATA. Modeled assumptions, no real merchants or money.";
  const banners = (
    <>
      <div className="bg-amber-100 px-3 py-1 text-center text-xs font-semibold text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">{banner}</div>
      {me?.kill_switch.global && <div role="alert" className="bg-red-600 px-3 py-1.5 text-center text-sm font-semibold text-white">The agent is disabled by the kill switch. Nothing will be acted on until an admin re-enables it.</div>}
      {me && me.kill_switch.users.includes(me.sub) && <div role="alert" className="bg-red-600 px-3 py-1.5 text-center text-sm font-semibold text-white">Agent actions are disabled for your account.</div>}
    </>
  );
  if (!me) return <div className="min-h-screen">{banners}<LoginScreen /></div>;

  const [, route, arg] = hash.split("/");
  let page;
  if (route === "case" && arg) page = can("case.read") ? <CasePage sid={arg} /> : null;
  else if (route === "approvals") page = <Approvals />;
  else if (route === "scoreboard") page = <Scoreboard />;
  else if (route === "lab") page = can("lab.run") ? <Lab /> : null;
  else if (route === "admin") page = can("killswitch.set") ? <Admin /> : null;
  else page = can("console.read") ? <Console /> : null;
  if (page === null) page = <p className="rounded-lg bg-slate-100 p-4 text-sm dark:bg-slate-800">Your role ({me.roles.join(", ")}) does not have access to this page.</p>;
  const active = route === "case" ? "#/" : `#/${route ?? ""}`;

  return (
    <div className="min-h-screen">
      {banners}
      <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-4 px-4 py-2">
          <a href="#/" className="text-lg font-bold tracking-tight">RIPPLE<span className="ml-1 hidden text-xs font-normal text-slate-500 sm:inline">resolves the issue, not the ticket</span></a>
          <nav className="flex flex-wrap gap-1">
            {NAV.filter(([, , perm]) => can(perm)).map(([h, label]) => (
              <a key={h} href={h} className={`rounded-md px-3 py-1.5 text-sm font-medium ${active === h ? "bg-indigo-600 text-white" : "hover:bg-slate-100 dark:hover:bg-slate-800"}`}>
                {label}{h === "#/approvals" && pending > 0 && <span className="ml-1.5 rounded-full bg-amber-500 px-1.5 text-xs text-white">{pending}</span>}
              </a>))}
          </nav>
          <div className="ml-auto flex items-center gap-2 text-xs">
            <span className="rounded-full bg-slate-100 px-2.5 py-1 dark:bg-slate-800" title={`tenant ${me.tenant}`}><b>{me.name}</b> · {me.roles.join(", ")}</span>
            {personas.length > 0 && (
              <select aria-label="Switch demo persona" className="rounded-md border border-slate-300 bg-transparent px-1.5 py-1 dark:border-slate-700" value="" onChange={(e) => e.target.value && login(e.target.value)}>
                <option value="">switch persona…</option>
                {personas.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>)}
            <button className="rounded-md border border-slate-300 px-2 py-1 dark:border-slate-700" onClick={logout}>Sign out</button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-5">{page}</main>
    </div>
  );
}
