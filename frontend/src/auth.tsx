import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, Me, PublicCfg, setToken } from "./api";
import { browserEnv, CallbackResult, completeLogin, endSessionUrl, OidcConfig, startLogin } from "./oidc";

type Source = "oidc" | "dev" | null;

interface AuthCtx {
  me: Me | null;
  cfg: PublicCfg | null;
  source: Source;
  oidc: OidcConfig | null;
  authError: string;
  can: (perm: string) => boolean;
  login: (persona: string) => Promise<void>;     // demo personas (RIPPLE_ENV=dev only)
  loginSso: () => Promise<void>;                 // real sign-in: authorization code + PKCE
  logout: () => void;
  refresh: () => void;
}
const Ctx = createContext<AuthCtx>({ me: null, cfg: null, source: null, oidc: null, authError: "", can: () => false, login: async () => {}, loginSso: async () => {}, logout: () => {}, refresh: () => {} });
export const useAuth = () => useContext(Ctx);

// Dev personas: only the persona NAME is remembered (to re-mint a 15-minute demo token). Never a token.
const KEY = "ripple.persona";
const remembered = () => { try { return localStorage.getItem(KEY); } catch { return null; } };
// Guard against redirect loops: at most one silent renewal attempt per 30 seconds.
const SILENT_KEY = "ripple.oidc.silent_at";
const SILENT_MIN_GAP_MS = 30_000;

// React StrictMode runs effects twice in development; the one-time authorization code must be consumed exactly once.
let callbackOnce: Promise<{ oidc: OidcConfig | null; result: CallbackResult; error: string }> | null = null;

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [cfg, setCfg] = useState<PublicCfg | null>(null);
  const [oidc, setOidc] = useState<OidcConfig | null>(null);
  const [source, setSource] = useState<Source>(null);
  const [authError, setAuthError] = useState("");
  const [ready, setReady] = useState(false);
  const idToken = useRef<string | undefined>(undefined);   // memory only; used as a logout hint

  const refresh = useCallback(() => { api.me().then(setMe).catch(() => setMe(null)); }, []);

  const login = useCallback(async (persona: string) => {
    const r = await api.devLogin(persona);
    setToken(r.token);
    try { localStorage.setItem(KEY, persona); } catch { /* storage may be blocked */ }
    setSource("dev");
    setAuthError("");
    setMe(await api.me());
  }, []);

  const loginSso = useCallback(async () => {
    if (!oidc) return;
    setAuthError("");
    try { await startLogin(oidc, browserEnv(), { returnHash: window.location.hash }); } catch (e) { setAuthError(e instanceof Error ? e.message : String(e)); }
  }, [oidc]);

  const logout = useCallback(() => {
    const url = source === "oidc" && oidc ? endSessionUrl(oidc, window.location.origin, idToken.current) : null;
    setToken(null); setMe(null); setSource(null); idToken.current = undefined;
    try { localStorage.removeItem(KEY); sessionStorage.removeItem(SILENT_KEY); } catch { /* ignore */ }
    if (url) window.location.assign(url);                    // also ends the IdP session so "Sign out" really signs out
  }, [source, oidc]);

  useEffect(() => {
    (async () => {
      try {
        const c = await api.publicConfig();
        setCfg(c);
        if (c.auth.oidc_login) {
          callbackOnce ??= (async () => {
            let oc: OidcConfig | null = null;
            let error = "";
            try { oc = await api.oidcConfig(); } catch (e) { error = e instanceof Error ? e.message : String(e); }
            const result: CallbackResult = oc ? await completeLogin(oc, browserEnv()) : { status: "none" };
            return { oidc: oc, result, error };
          })();
          const { oidc: oc, result, error } = await callbackOnce;
          setOidc(oc);
          if (error) setAuthError(`Single sign-on is not available: ${error}`);
          if (result.status === "ok") {
            setToken(result.tokens.accessToken);
            idToken.current = result.tokens.idToken;
            try {
              setMe(await api.me());
              setSource("oidc");
              if (result.returnHash) window.location.hash = result.returnHash;
            } catch (e) {
              setToken(null);
              setAuthError(`The API refused your sign-in: ${e instanceof Error ? e.message : String(e)}`);
            }
          } else if (result.status === "error") {
            setAuthError(result.message);
            if (result.returnHash) window.location.hash = result.returnHash;
          }
        }
        const p = remembered();
        if (c.auth.dev_login && p && !hasSession()) { try { await login(p); } catch { /* show picker */ } }
      } catch { /* leave the sign-in screen to explain */ } finally { setReady(true); }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const hasSession = () => { try { return api.hasToken(); } catch { return false; } };

  // A 401 anywhere means the short-lived token expired or was revoked.
  useEffect(() => {
    const onAuth = async () => {
      if (source === "dev") { const p = remembered(); if (p) { try { await login(p); return; } catch { /* fall through */ } } }
      if (source === "oidc" && oidc) {
        let last = 0;
        try { last = Number(sessionStorage.getItem(SILENT_KEY) || 0); } catch { /* ignore */ }
        setToken(null); setMe(null);
        if (Date.now() - last > SILENT_MIN_GAP_MS) {      // silent renewal against the IdP session (prompt=none)
          try { sessionStorage.setItem(SILENT_KEY, String(Date.now())); } catch { /* ignore */ }
          try { await startLogin(oidc, browserEnv(), { silent: true, returnHash: window.location.hash }); return; } catch { /* show sign-in */ }
        }
        setAuthError("Your session expired. Please sign in again.");
        return;
      }
      setToken(null); setMe(null);
    };
    window.addEventListener("ripple-auth", onAuth);
    return () => window.removeEventListener("ripple-auth", onAuth);
  }, [source, oidc, login]);

  useEffect(() => { if (!me) return; const t = setInterval(refresh, 15000); return () => clearInterval(t); }, [me, refresh]);

  const can = useCallback((perm: string) => !!me?.perms.includes(perm), [me]);
  if (!ready) return <p className="p-8 text-sm text-slate-500">Loading…</p>;
  return <Ctx.Provider value={{ me, cfg, source, oidc, authError, can, login, loginSso, logout, refresh }}>{children}</Ctx.Provider>;
}

const ROLE_BLURB: Record<string, string> = {
  priya: "Risk analyst. Approves or rejects Tier 3 actions. Cannot run demo controls.",
  sam: "Ops admin. Runs demo controls, kill switch, audit checks. Cannot approve risk actions (separation of duties).",
  meena: "Read-only viewer. Sees dashboards with personal data masked.",
};

export function LoginScreen() {
  const { cfg, oidc, authError, login, loginSso } = useAuth();
  const [personas, setPersonas] = useState<{ id: string; name: string }[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (cfg?.auth.dev_login) api.personas().then(setPersonas).catch((e) => setErr(String(e))); }, [cfg]);

  const sso = cfg?.auth.oidc_login;
  const dev = cfg?.auth.dev_login;
  if (cfg && !sso && !dev) {
    return (
      <div className="mx-auto max-w-lg p-8">
        <h1 className="text-xl font-semibold">Sign-in required</h1>
        <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
          {cfg.auth.oidc_configured
            ? "The API validates tokens from your identity provider, but browser sign-in is not configured (RIPPLE_OIDC_CLIENT_ID is missing)."
            : "This deployment accepts short-lived OIDC access tokens from your company identity provider, but none is configured (RIPPLE_OIDC_ISSUER / RIPPLE_OIDC_AUDIENCE), so every request is refused."}
        </p>
        <p className="mt-2 text-xs text-slate-500">There is no password form and no password store.</p>
      </div>
    );
  }
  return (
    <div className="mx-auto max-w-2xl p-8">
      <h1 className="text-xl font-semibold">Sign in to RIPPLE</h1>
      {authError && <p role="alert" className="mt-3 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200">{authError}</p>}
      {sso && (
        <div className="mt-4 rounded-xl border border-slate-300 p-4 dark:border-slate-700">
          <p className="text-sm text-slate-600 dark:text-slate-300">Use your company single sign-on. You are sent to your identity provider and returned here with a short-lived token held in this tab's memory only.</p>
          <button disabled={busy || !oidc} onClick={async () => { setBusy(true); await loginSso(); setBusy(false); }}
            className="mt-3 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50">
            Sign in with SSO{oidc ? ` (${new URL(oidc.issuer).host})` : ""}
          </button>
        </div>
      )}
      {dev && (
        <div className="mt-6">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Demo personas <span className="font-normal normal-case">(RIPPLE_ENV=dev only; 15-minute tokens)</span></h2>
          {err && <p className="mt-2 text-xs text-red-600">{err}</p>}
          <div className="mt-3 grid gap-3 sm:grid-cols-3">
            {personas.map((p) => (
              <button key={p.id} onClick={() => login(p.id).catch((e) => setErr(String(e)))}
                className="rounded-xl border border-slate-300 p-3 text-left hover:border-indigo-500 hover:bg-indigo-50 dark:border-slate-700 dark:hover:bg-indigo-950/40">
                <span className="block font-semibold">{p.name}</span>
                <span className="mt-1 block text-xs text-slate-500">{ROLE_BLURB[p.id]}</span>
              </button>))}
          </div>
        </div>
      )}
    </div>
  );
}
