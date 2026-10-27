import { spawn, ChildProcess } from "node:child_process";
import { webcrypto } from "node:crypto";
import net from "node:net";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import * as oidc from "../src/oidc";
import type { Env, OidcConfig } from "../src/oidc";

// ------------------------------------------------------------------ helpers
type FakeEnv = Env & { store: Map<string, string>; navigated: string[]; replaced: string[]; clock: { t: number } };

function fakeEnv(over: Partial<Env> = {}): FakeEnv {
  const store = new Map<string, string>();
  const navigated: string[] = [];
  const replaced: string[] = [];
  const clock = { t: 1_700_000_000_000 };
  return {
    storage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => void store.set(k, v), removeItem: (k) => void store.delete(k) },
    assign: (u) => void navigated.push(u),
    replaceUrl: (p) => void replaced.push(p),
    origin: "http://localhost:5173", pathname: "/", hash: "#/approvals", search: "",
    now: () => clock.t,
    fetch: (() => { throw new Error("no network in this test"); }) as unknown as typeof fetch,
    crypto: webcrypto as unknown as Crypto,
    store, navigated, replaced, clock, ...over,
  };
}

const cfgFor = (base = "https://idp.example.com"): OidcConfig => ({
  issuer: base, client_id: "ripple-spa", scope: "openid profile", auth_params: "",
  authorization_endpoint: `${base}/authorize`, token_endpoint: `${base}/token`, end_session_endpoint: `${base}/logout`,
});

const b64 = (o: unknown) => oidc.base64url(new TextEncoder().encode(JSON.stringify(o)));
const jwtOf = (payload: Record<string, unknown>) => `${b64({ alg: "RS256", typ: "JWT" })}.${b64(payload)}.sig`;
const pending = (env: FakeEnv) => JSON.parse(env.store.get("ripple.oidc.pending")!);
const authParams = (env: FakeEnv) => new URL(env.navigated.at(-1)!).searchParams;

function tokenFetch(env: FakeEnv, cfg: OidcConfig, over: Record<string, unknown> = {}, idOver: Record<string, unknown> = {}, status = 200) {
  const calls: { url: string; init: RequestInit }[] = [];
  const p = pending(env);
  const id = jwtOf({ iss: cfg.issuer, aud: cfg.client_id, sub: "u", exp: Math.floor(env.clock.t / 1000) + 300, nonce: p.nonce, ...idOver });
  const body = { access_token: "at-123", id_token: id, token_type: "Bearer", expires_in: 600, refresh_token: "must-be-dropped", ...over };
  env.fetch = (async (url: string, init: RequestInit) => { calls.push({ url, init }); return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }); }) as unknown as typeof fetch;
  return calls;
}

async function begin(cfg = cfgFor(), env = fakeEnv(), silent = false) {
  await oidc.startLogin(cfg, env, { silent });
  return { cfg, env, state: authParams(env).get("state")! };
}

// ------------------------------------------------------------------ PKCE primitives
describe("PKCE primitives", () => {
  it("matches the RFC 7636 appendix B test vector", async () => {
    expect(await oidc.challengeFor(webcrypto as unknown as Crypto, "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")).toBe("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
  });
  it("makes url-safe, unpadded, unpredictable verifiers within RFC length limits", () => {
    const c = webcrypto as unknown as Crypto;
    const a = oidc.randomString(c, 32), b = oidc.randomString(c, 32);
    expect(a).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(a).not.toBe(b);
  });
  it("refuses to run without WebCrypto's digest (insecure context)", async () => {
    await expect(oidc.challengeFor({ getRandomValues: () => {} } as unknown as Crypto, "x")).rejects.toThrow(/secure context/);
  });
});

// ------------------------------------------------------------------ authorization request
describe("authorization request", () => {
  it("is a code + S256 PKCE request with state and nonce, and no secret", async () => {
    const { env } = await begin();
    const q = authParams(env);
    expect(q.get("response_type")).toBe("code");
    expect(q.get("code_challenge_method")).toBe("S256");
    expect(q.get("client_id")).toBe("ripple-spa");
    expect(q.get("redirect_uri")).toBe("http://localhost:5173/");
    expect(q.get("scope")).toBe("openid profile");
    expect(q.get("state")!.length).toBeGreaterThanOrEqual(20);
    expect(q.get("nonce")!.length).toBeGreaterThanOrEqual(20);
    expect([...q.keys()].some((k) => /secret|password|token/i.test(k))).toBe(false);
  });
  it("sends only the challenge; the verifier stays in the tab and nothing else is stored", async () => {
    const { env } = await begin();
    const p = pending(env);
    expect(env.navigated[0]).not.toContain(p.verifier);
    expect(authParams(env).get("code_challenge")).toBe(await oidc.challengeFor(webcrypto as unknown as Crypto, p.verifier));
    expect([...env.store.keys()]).toEqual(["ripple.oidc.pending"]);
    expect(p.returnHash).toBe("#/approvals");
  });
  it("uses fresh state, nonce and verifier on every attempt", async () => {
    const a = await begin(), b = await begin();
    expect(a.state).not.toBe(b.state);
    expect(pending(a.env).verifier).not.toBe(pending(b.env).verifier);
  });
  it("lets deployment extras add params (audience) but never override the security-critical ones", async () => {
    const cfg = { ...cfgFor(), auth_params: "audience=ripple-api&state=evil&code_challenge=evil&redirect_uri=http://evil&client_id=x&code_challenge_method=plain&scope=admin" };
    const { env } = await begin(cfg);
    const q = authParams(env);
    expect(q.get("audience")).toBe("ripple-api");
    expect(q.get("state")).not.toBe("evil");
    expect(q.get("code_challenge")).not.toBe("evil");
    expect(q.get("code_challenge_method")).toBe("S256");
    expect(q.get("redirect_uri")).toBe("http://localhost:5173/");
    expect(q.get("client_id")).toBe("ripple-spa");
    expect(q.get("scope")).toBe("openid profile");
  });
  it("adds prompt=none for silent renewal and refuses to let extras change it", async () => {
    const cfg = { ...cfgFor(), auth_params: "prompt=login" };
    expect(authParams((await begin(cfg, fakeEnv(), true)).env).get("prompt")).toBe("none");
    expect(authParams((await begin(cfg)).env).get("prompt")).toBe("login");
  });
  it("only talks to https endpoints (http allowed for loopback while developing)", async () => {
    await expect(oidc.startLogin({ ...cfgFor(), authorization_endpoint: "http://idp.example.com/authorize" }, fakeEnv())).rejects.toThrow(/https/);
    await expect(oidc.startLogin({ ...cfgFor(), token_endpoint: "http://idp.example.com/token" }, fakeEnv())).rejects.toThrow(/https/);
    await expect(oidc.startLogin(cfgFor("http://127.0.0.1:9100"), fakeEnv())).resolves.toBeUndefined();
    await expect(oidc.startLogin(cfgFor("http://127.0.0.1:9100"), fakeEnv({ origin: "https://app.example.com" }))).rejects.toThrow(/https/);
  });
});

// ------------------------------------------------------------------ callback handling
describe("callback", () => {
  it("parses code, error and nothing", () => {
    expect(oidc.parseCallback("")).toEqual({ kind: "none" });
    expect(oidc.parseCallback("?code=abc&state=s&iss=https%3A%2F%2Fidp")).toMatchObject({ kind: "code", code: "abc", state: "s", iss: "https://idp" });
    expect(oidc.parseCallback("?error=access_denied&error_description=no&state=s")).toMatchObject({ kind: "error", error: "access_denied" });
  });

  it("completes a valid sign-in: exchanges the code with the verifier, validates the ID token, drops the refresh token", async () => {
    const { cfg, env, state } = await begin();
    const verifier = pending(env).verifier;
    const calls = tokenFetch(env, cfg);
    env.search = `?code=the-code&state=${state}`;
    const r = await oidc.completeLogin(cfg, env);
    expect(r.status).toBe("ok");
    if (r.status !== "ok") return;
    expect(r.tokens.accessToken).toBe("at-123");
    expect(r.tokens.expiresAt).toBe(env.clock.t + 600_000);
    expect(r.returnHash).toBe("#/approvals");
    expect(JSON.stringify(r)).not.toContain("must-be-dropped");
    const body = new URLSearchParams(calls[0].init.body as URLSearchParams);
    expect(body.get("grant_type")).toBe("authorization_code");
    expect(body.get("code")).toBe("the-code");
    expect(body.get("code_verifier")).toBe(verifier);
    expect(body.get("redirect_uri")).toBe("http://localhost:5173/");
    expect(body.has("client_secret")).toBe(false);
    expect(calls[0].init.credentials).toBe("omit");
    expect(calls[0].init.redirect).toBe("error");
  });

  it("removes the code from the address bar and the pending record, even on failure", async () => {
    const ok = await begin();
    tokenFetch(ok.env, ok.cfg);
    ok.env.search = `?code=c&state=${ok.state}`;
    await oidc.completeLogin(ok.cfg, ok.env);
    expect(ok.env.replaced).toEqual(["/#/approvals"]);
    expect(ok.env.store.size).toBe(0);
    const bad = await begin();
    bad.env.search = `?code=c&state=WRONG`;
    await oidc.completeLogin(bad.cfg, bad.env);
    expect(bad.env.replaced).toEqual(["/#/approvals"]);
    expect(bad.env.store.size).toBe(0);
  });

  it("rejects a state mismatch without ever calling the token endpoint (CSRF / login injection)", async () => {
    const { cfg, env } = await begin();
    let called = false;
    env.fetch = (async () => { called = true; return new Response("{}"); }) as unknown as typeof fetch;
    env.search = "?code=c&state=attacker-chosen";
    const r = await oidc.completeLogin(cfg, env);
    expect(r).toMatchObject({ status: "error" });
    expect((r as { message: string }).message).toMatch(/state/);
    expect(called).toBe(false);
  });

  it("is single use: replaying the same callback finds no sign-in in progress", async () => {
    const { cfg, env, state } = await begin();
    tokenFetch(env, cfg);
    env.search = `?code=c&state=${state}`;
    expect((await oidc.completeLogin(cfg, env)).status).toBe("ok");
    const again = await oidc.completeLogin(cfg, env);
    expect(again.status).toBe("error");
    expect((again as { message: string }).message).toMatch(/No sign-in was in progress/);
  });

  it("rejects callbacks with no pending attempt, an expired one, or a wrong issuer (mix-up, RFC 9207)", async () => {
    const stray = fakeEnv({ search: "?code=c&state=s" });
    expect(((await oidc.completeLogin(cfgFor(), stray)) as { message: string }).message).toMatch(/No sign-in/);
    const { cfg, env, state } = await begin();
    env.clock.t += 11 * 60 * 1000;
    env.search = `?code=c&state=${state}`;
    expect(((await oidc.completeLogin(cfg, env)) as { message: string }).message).toMatch(/too long/);
    const m = await begin();
    m.env.search = `?code=c&state=${m.state}&iss=${encodeURIComponent("https://evil.example.com")}`;
    expect(((await oidc.completeLogin(m.cfg, m.env)) as { message: string }).message).toMatch(/unexpected issuer/);
  });

  it("explains IdP errors, and flags a failed silent renewal so the UI shows the sign-in screen", async () => {
    const silent = await begin(cfgFor(), fakeEnv(), true);
    silent.env.search = `?error=login_required&state=${silent.state}`;
    expect(await oidc.completeLogin(silent.cfg, silent.env)).toMatchObject({ status: "error", silentFailed: true });
    const interactive = await begin();
    interactive.env.search = `?error=access_denied&error_description=user%20said%20no&state=${interactive.state}`;
    const r = await oidc.completeLogin(interactive.cfg, interactive.env);
    expect(r).toMatchObject({ status: "error", silentFailed: false });
    expect((r as { message: string }).message).toMatch(/access_denied/);
  });

  it.each([
    ["nonce mismatch (replayed ID token)", { nonce: "someone-elses" }, /nonce/],
    ["audience mismatch", { aud: "another-client" }, /audience/],
    ["issuer mismatch", { iss: "https://evil.example.com" }, /issuer/],
    ["expired ID token", { exp: 1 }, /expired/],
  ])("rejects an ID token with %s", async (_n, idOver, re) => {
    const { cfg, env, state } = await begin();
    tokenFetch(env, cfg, {}, idOver as Record<string, unknown>);
    env.search = `?code=c&state=${state}`;
    const r = await oidc.completeLogin(cfg, env);
    expect(r.status).toBe("error");
    expect((r as { message: string }).message).toMatch(re);
  });

  it("surfaces token-endpoint rejections, network failures, and unusable responses", async () => {
    const a = await begin();
    tokenFetch(a.env, a.cfg, { error: "invalid_grant", error_description: "PKCE verification failed" }, {}, 400);
    a.env.search = `?code=c&state=${a.state}`;
    expect(((await oidc.completeLogin(a.cfg, a.env)) as { message: string }).message).toMatch(/invalid_grant/);
    const b = await begin();
    b.env.fetch = (async () => { throw new TypeError("network"); }) as unknown as typeof fetch;
    b.env.search = `?code=c&state=${b.state}`;
    expect(((await oidc.completeLogin(b.cfg, b.env)) as { message: string }).message).toMatch(/Could not reach/);
    const c = await begin();
    tokenFetch(c.env, c.cfg, { token_type: "mac" });
    c.env.search = `?code=c&state=${c.state}`;
    expect(((await oidc.completeLogin(c.cfg, c.env)) as { message: string }).message).toMatch(/token type/);
    const d = await begin();
    tokenFetch(d.env, d.cfg, { access_token: "" });
    d.env.search = `?code=c&state=${d.state}`;
    expect(((await oidc.completeLogin(d.cfg, d.env)) as { message: string }).message).toMatch(/no access token/);
  });

  it("builds a logout URL that carries the ID token hint and a registered return address", () => {
    const u = new URL(oidc.endSessionUrl(cfgFor(), "http://localhost:5173", "idt")!);
    expect(u.searchParams.get("id_token_hint")).toBe("idt");
    expect(u.searchParams.get("post_logout_redirect_uri")).toBe("http://localhost:5173/");
    expect(oidc.endSessionUrl({ ...cfgFor(), end_session_endpoint: undefined }, "http://x")).toBeNull();
  });
});

// ------------------------------------------------------------------ against the real (strict) mock IdP
const PY = process.env.PYTHON ?? "python";
let proc: ChildProcess | undefined;
let base = "";
let available = false;
let realCfg: OidcConfig;

async function freePort(): Promise<number> {
  return new Promise((res, rej) => { const s = net.createServer(); s.listen(0, "127.0.0.1", () => { const p = (s.address() as net.AddressInfo).port; s.close(() => res(p)); }); s.on("error", rej); });
}

beforeAll(async () => {
  try {
    const port = await freePort();
    base = `http://127.0.0.1:${port}`;
    proc = spawn(PY, ["-W", "ignore", "-m", "tests.mock_idp", "--port", String(port)], {
      cwd: fileURLToPath(new URL("../../backend", import.meta.url)), env: { ...process.env, RIPPLE_ENV: "dev" }, stdio: "ignore",
    });
    for (let i = 0; i < 120 && !available; i++) {
      try { const r = await fetch(`${base}/.well-known/openid-configuration`); if (r.ok) { const d = await r.json(); realCfg = { issuer: d.issuer, client_id: "ripple-spa", scope: "openid profile", auth_params: "", authorization_endpoint: d.authorization_endpoint, token_endpoint: d.token_endpoint, end_session_endpoint: d.end_session_endpoint }; available = true; } } catch { /* not up yet */ }
      if (!available) await new Promise((r) => setTimeout(r, 250));
    }
  } catch { available = false; }
}, 60_000);

afterAll(() => { proc?.kill(); });

/** Plays the browser between the app and the IdP: follows the authorize redirect (with the IdP session cookie) and returns the callback query. */
async function viaIdp(env: FakeEnv, user?: string, cookie?: string): Promise<{ search: string; cookie?: string; status: number }> {
  const url = new URL(env.navigated.at(-1)!);
  if (user) url.searchParams.set("login_hint", user);
  const r = await fetch(url, { redirect: "manual", headers: cookie ? { cookie } : {} });
  const loc = r.headers.get("location");
  const setCookie = r.headers.get("set-cookie")?.split(";")[0];
  return { search: loc ? new URL(loc).search : "", cookie: setCookie ?? cookie, status: r.status };
}

describe("against the strict mock IdP (real HTTP, real PKCE verification)", () => {
  const live = (name: string, fn: () => Promise<void>) => it(name, async (ctx) => { if (!available) ctx.skip(); await fn(); });

  live("signs in end to end: the IdP verifies our S256 challenge, state round-trips, nonce is honoured", async () => {
    const env = fakeEnv({ fetch: globalThis.fetch });
    await oidc.startLogin(realCfg, env);
    const cb = await viaIdp(env, "priya");
    expect(cb.status).toBe(302);
    env.search = cb.search;
    const r = await oidc.completeLogin(realCfg, env);
    expect(r.status).toBe("ok");
    if (r.status !== "ok") return;
    const claims = oidc.decodeJwtPayload(r.tokens.accessToken);
    expect(claims).toMatchObject({ sub: "idp|priya", aud: "ripple-api", roles: ["analyst"] });
    expect(r.tokens.accessToken.split(".")).toHaveLength(3);
    expect(env.store.size).toBe(0);
  });

  live("a tampered code_verifier is refused by the IdP and surfaced to the user", async () => {
    const env = fakeEnv({ fetch: globalThis.fetch });
    await oidc.startLogin(realCfg, env);
    const cb = await viaIdp(env, "priya");
    const p = pending(env);
    env.store.set("ripple.oidc.pending", JSON.stringify({ ...p, verifier: oidc.randomString(webcrypto as unknown as Crypto, 32) }));
    env.search = cb.search;
    const r = await oidc.completeLogin(realCfg, env);
    expect(r.status).toBe("error");
    expect((r as { message: string }).message).toMatch(/invalid_grant/);
  });

  live("a tampered state is rejected before any token request", async () => {
    const env = fakeEnv({ fetch: globalThis.fetch });
    await oidc.startLogin(realCfg, env);
    const cb = await viaIdp(env, "priya");
    env.search = cb.search.replace(/state=[^&]+/, "state=forged");
    const r = await oidc.completeLogin(realCfg, env);
    expect((r as { message: string }).message).toMatch(/state/);
  });

  live("a replayed ID token (wrong nonce) is rejected", async () => {
    const env = fakeEnv({ fetch: globalThis.fetch });
    await oidc.startLogin(realCfg, env);
    const cb = await viaIdp(env, "priya");
    env.store.set("ripple.oidc.pending", JSON.stringify({ ...pending(env), nonce: "a-different-nonce" }));
    env.search = cb.search;
    const r = await oidc.completeLogin(realCfg, env);
    expect((r as { message: string }).message).toMatch(/nonce/);
  });

  live("silent renewal: no IdP session gives login_required, an existing session renews without a prompt", async () => {
    const e1 = fakeEnv({ fetch: globalThis.fetch });
    await oidc.startLogin(realCfg, e1, { silent: true });
    const none = await viaIdp(e1);
    expect(none.search).toContain("error=login_required");
    e1.search = none.search;
    expect(await oidc.completeLogin(realCfg, e1)).toMatchObject({ status: "error", silentFailed: true });

    const first = fakeEnv({ fetch: globalThis.fetch });
    await oidc.startLogin(realCfg, first);
    const session = await viaIdp(first, "sam");
    expect(session.cookie).toContain("idp_session=");

    const again = fakeEnv({ fetch: globalThis.fetch });
    await oidc.startLogin(realCfg, again, { silent: true });
    const renewed = await viaIdp(again, undefined, session.cookie);
    again.search = renewed.search;
    const r = await oidc.completeLogin(realCfg, again);
    expect(r.status).toBe("ok");
    if (r.status === "ok") expect(oidc.decodeJwtPayload(r.tokens.accessToken).sub).toBe("idp|sam");
  });

  live("the IdP refuses an unregistered redirect URI outright (it never redirects there)", async () => {
    const env = fakeEnv({ fetch: globalThis.fetch, origin: "http://localhost:5199" });
    await oidc.startLogin(realCfg, env);
    const cb = await viaIdp(env, "priya");
    expect(cb.status).toBe(400);
    expect(cb.search).toBe("");
  });
});
