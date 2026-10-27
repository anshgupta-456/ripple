import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { api, Me, PublicCfg, setToken } from "./api";

interface AuthCtx {
  me: Me | null;
  cfg: PublicCfg | null;
  can: (perm: string) => boolean;
  login: (persona: string) => Promise<void>;
  logout: () => void;
  refresh: () => void;
}
const Ctx = createContext<AuthCtx>({ me: null, cfg: null, can: () => false, login: async () => {}, logout: () => {}, refresh: () => {} });
export const useAuth = () => useContext(Ctx);

// Only the persona NAME is remembered (to re-sign-in after a 15-minute token expires in demo mode). Never the token.
const KEY = "ripple.persona";
const remembered = () => { try { return localStorage.getItem(KEY); } catch { return null; } };

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [cfg, setCfg] = useState<PublicCfg | null>(null);
  const [ready, setReady] = useState(false);

  const refresh = useCallback(() => { api.me().then(setMe).catch(() => setMe(null)); }, []);

  const login = useCallback(async (persona: string) => {
    const r = await api.devLogin(persona);
    setToken(r.token);
    try { localStorage.setItem(KEY, persona); } catch { /* storage may be blocked */ }
    setMe(await api.me());
  }, []);

  const logout = useCallback(() => {
    setToken(null); setMe(null);
    try { localStorage.removeItem(KEY); } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    api.publicConfig().then(async (c) => {
      setCfg(c);
      const p = remembered();
      if (c.auth.dev_login && p) { try { await login(p); } catch { /* show picker */ } }
    }).catch(() => {}).finally(() => setReady(true));
  }, [login]);

  // an expired/invalid token anywhere in the app: silently re-mint in demo mode, otherwise require sign-in again
  useEffect(() => {
    const onAuth = async () => {
      const p = remembered();
      if (cfg?.auth.dev_login && p) { try { await login(p); return; } catch { /* fall through */ } }
      setToken(null); setMe(null);
    };
    window.addEventListener("ripple-auth", onAuth);
    return () => window.removeEventListener("ripple-auth", onAuth);
  }, [cfg, login]);

  useEffect(() => { if (!me) return; const t = setInterval(refresh, 15000); return () => clearInterval(t); }, [me, refresh]);

  const can = useCallback((perm: string) => !!me?.perms.includes(perm), [me]);
  if (!ready) return <p className="p-8 text-sm text-slate-500">Loading…</p>;
  return <Ctx.Provider value={{ me, cfg, can, login, logout, refresh }}>{children}</Ctx.Provider>;
}

const ROLE_BLURB: Record<string, string> = {
  priya: "Risk analyst. Approves or rejects Tier 3 actions. Cannot run demo controls.",
  sam: "Ops admin. Runs demo controls, kill switch, audit checks. Cannot approve risk actions (separation of duties).",
  meena: "Read-only viewer. Sees dashboards with personal data masked.",
};

export function LoginScreen() {
  const { cfg, login } = useAuth();
  const [personas, setPersonas] = useState<{ id: string; name: string; roles: string[] }[]>([]);
  const [err, setErr] = useState("");
  useEffect(() => { if (cfg?.auth.dev_login) api.personas().then(setPersonas).catch((e) => setErr(String(e))); }, [cfg]);

  if (cfg && !cfg.auth.dev_login) {
    return (
      <div className="mx-auto max-w-lg p-8">
        <h1 className="text-xl font-semibold">Sign-in required</h1>
        <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
          This deployment accepts short-lived OIDC access tokens from your company identity provider
          {cfg.auth.oidc_configured ? "." : ", but none is configured (RIPPLE_OIDC_ISSUER / RIPPLE_OIDC_AUDIENCE), so every request is refused."}
        </p>
        <p className="mt-2 text-xs text-slate-500">The browser sign-in flow (authorization code + PKCE) is deployment-specific and is not part of this prototype. There is no password form and no password store.</p>
      </div>
    );
  }
  return (
    <div className="mx-auto max-w-2xl p-8">
      <h1 className="text-xl font-semibold">RIPPLE: sign in (demo mode)</h1>
      <p className="mt-1 text-sm text-slate-500">Pick a persona. Each gets a 15-minute token with only that role's permissions. In production this screen is replaced by your company SSO.</p>
      {err && <p className="mt-2 text-xs text-red-600">{err}</p>}
      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        {personas.map((p) => (
          <button key={p.id} onClick={() => login(p.id).catch((e) => setErr(String(e)))}
            className="rounded-xl border border-slate-300 p-3 text-left hover:border-indigo-500 hover:bg-indigo-50 dark:border-slate-700 dark:hover:bg-indigo-950/40">
            <span className="block font-semibold">{p.name}</span>
            <span className="text-xs uppercase tracking-wide text-indigo-600">{p.roles.join(", ")}</span>
            <span className="mt-1 block text-xs text-slate-500">{ROLE_BLURB[p.id]}</span>
          </button>))}
      </div>
    </div>
  );
}
