import { useEffect, useState } from "react";
import { api } from "./api";
import { usePolling } from "./components/ui";
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

const NAV: [string, string][] = [["#/", "Ops Console"], ["#/approvals", "Approvals"], ["#/scoreboard", "Scoreboard"], ["#/lab", "Lab"]];

export default function App() {
  const hash = useHash();
  const [pending, setPending] = useState(0);
  const [banner, setBanner] = useState("SYNTHETIC DATA. Modeled assumptions, no real merchants or money.");
  usePolling(() => { api.pending().then((p) => setPending(p.length)).catch(() => {}); }, 5000);
  useEffect(() => { api.config().then((c) => setBanner(c.banner)).catch(() => {}); }, []);

  const [, route, arg] = hash.split("/");
  let page;
  if (route === "case" && arg) page = <CasePage sid={arg} />;
  else if (route === "approvals") page = <Approvals />;
  else if (route === "scoreboard") page = <Scoreboard />;
  else if (route === "lab") page = <Lab />;
  else page = <Console />;
  const active = route === "case" ? "#/" : `#/${route ?? ""}`;

  return (
    <div className="min-h-screen">
      <div className="bg-amber-100 px-3 py-1 text-center text-xs font-semibold text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">{banner}</div>
      <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-4 px-4 py-2">
          <a href="#/" className="text-lg font-bold tracking-tight">RIPPLE<span className="ml-1 text-xs font-normal text-slate-500">resolves the issue, not the ticket</span></a>
          <nav className="flex gap-1">
            {NAV.map(([h, label]) => (
              <a key={h} href={h} className={`rounded-md px-3 py-1.5 text-sm font-medium ${active === h ? "bg-indigo-600 text-white" : "hover:bg-slate-100 dark:hover:bg-slate-800"}`}>
                {label}{h === "#/approvals" && pending > 0 && <span className="ml-1.5 rounded-full bg-amber-500 px-1.5 text-xs text-white">{pending}</span>}
              </a>))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-5">{page}</main>
    </div>
  );
}
