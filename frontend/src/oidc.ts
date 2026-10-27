// OpenID Connect authorization-code flow with PKCE (RFC 7636, OAuth 2.0 for Browser-Based Apps) for a PUBLIC client.
//
// Design rules, each one deliberate:
//  * Authorization code + PKCE (S256) only. No implicit flow, no client secret (a browser cannot keep one).
//  * `state` (CSRF) and `nonce` (ID-token replay) are random per attempt, bound to the attempt, and single-use.
//  * The access token is returned to the caller to hold IN MEMORY. This module never writes tokens anywhere.
//    Only the short-lived PKCE verifier/state/nonce and the page to return to are kept in sessionStorage across the redirect.
//  * Refresh tokens are ignored if an IdP sends one. Renewal is a silent re-authorization (prompt=none) against the IdP session.
//  * The authorization code is stripped from the address bar before anything else happens.
//  * The ID token arrives straight from the token endpoint over TLS, so (OIDC Core 3.1.3.7) its signature need not be checked
//    here; we check iss, aud, exp and nonce. The API independently verifies the ACCESS token's signature on every request.
//  * Endpoints must be https (http only for loopback during local development).

export interface OidcConfig {
  issuer: string;
  client_id: string;
  scope: string;
  auth_params: string;
  authorization_endpoint: string;
  token_endpoint: string;
  end_session_endpoint?: string;
}

export interface TokenSet {
  accessToken: string;
  idToken?: string;
  expiresAt: number; // ms since epoch
}

export type CallbackResult =
  | { status: "none" }
  | { status: "ok"; tokens: TokenSet; returnHash: string }
  | { status: "error"; message: string; silentFailed: boolean; returnHash: string };

/** Everything the flow needs from the browser, injectable so it can be tested without one. */
export interface Env {
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  assign(url: string): void;
  replaceUrl(pathAndHash: string): void;
  origin: string;
  pathname: string;
  hash: string;
  search: string;
  now(): number;
  fetch: typeof fetch;
  crypto: Crypto;
}

export const browserEnv = (): Env => ({
  storage: window.sessionStorage,
  assign: (u) => window.location.assign(u),
  replaceUrl: (p) => window.history.replaceState(null, "", p),
  origin: window.location.origin,
  pathname: window.location.pathname,
  hash: window.location.hash,
  search: window.location.search,
  now: () => Date.now(),
  fetch: (...a) => window.fetch(...a),
  crypto: window.crypto,
});

export class OidcClientError extends Error {}

const PENDING_KEY = "ripple.oidc.pending";
const PENDING_TTL_MS = 10 * 60 * 1000;
const RESERVED = new Set(["response_type", "client_id", "redirect_uri", "scope", "state", "nonce", "code_challenge", "code_challenge_method"]);
const SILENT_ERRORS = new Set(["login_required", "interaction_required", "consent_required", "account_selection_required"]);

// ------------------------------------------------------------------ primitives
export function base64url(bytes: Uint8Array): string {
  let s = "";
  bytes.forEach((b) => (s += String.fromCharCode(b)));
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function randomString(c: Crypto, bytes = 32): string {
  const b = new Uint8Array(bytes);
  c.getRandomValues(b);
  return base64url(b);
}

export async function challengeFor(c: Crypto, verifier: string): Promise<string> {
  if (!c.subtle) throw new OidcClientError("Secure sign-in needs a secure context (https, or localhost).");
  return base64url(new Uint8Array(await c.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
}

export async function pkcePair(c: Crypto): Promise<{ verifier: string; challenge: string }> {
  const verifier = randomString(c, 32); // 43 chars, within RFC 7636's 43-128
  return { verifier, challenge: await challengeFor(c, verifier) };
}

export const redirectUriFor = (origin: string) => `${origin}/`;

const isLoopback = (host: string) => host === "localhost" || host === "127.0.0.1" || host === "[::1]" || host === "::1";

export function assertSecureEndpoints(cfg: OidcConfig, origin: string): void {
  const pageIsHttps = origin.startsWith("https://");
  for (const [name, url] of [["authorization_endpoint", cfg.authorization_endpoint], ["token_endpoint", cfg.token_endpoint]] as const) {
    let u: URL;
    try { u = new URL(url); } catch { throw new OidcClientError(`${name} is not a valid URL`); }
    if (u.protocol === "https:") continue;
    if (u.protocol === "http:" && !pageIsHttps && isLoopback(u.hostname)) continue; // local development only
    throw new OidcClientError(`${name} must be https`);
  }
}

// ------------------------------------------------------------------ authorization request
export function buildAuthorizationUrl(cfg: OidcConfig, o: { state: string; nonce: string; challenge: string; redirectUri: string; silent?: boolean }): string {
  const q = new URLSearchParams();
  q.set("response_type", "code");
  q.set("client_id", cfg.client_id);
  q.set("redirect_uri", o.redirectUri);
  q.set("scope", cfg.scope);
  q.set("state", o.state);
  q.set("nonce", o.nonce);
  q.set("code_challenge", o.challenge);
  q.set("code_challenge_method", "S256");
  // deployment-specific extras (e.g. audience=...) may add parameters but can never override the security-critical ones
  for (const [k, v] of new URLSearchParams(cfg.auth_params || "")) {
    if (!RESERVED.has(k) && !(o.silent && k === "prompt")) q.set(k, v);
  }
  if (o.silent) q.set("prompt", "none");
  return `${cfg.authorization_endpoint}${cfg.authorization_endpoint.includes("?") ? "&" : "?"}${q.toString()}`;
}

export async function startLogin(cfg: OidcConfig, env: Env, opts: { silent?: boolean; returnHash?: string } = {}): Promise<void> {
  assertSecureEndpoints(cfg, env.origin);
  const { verifier, challenge } = await pkcePair(env.crypto);
  const state = randomString(env.crypto, 16);
  const nonce = randomString(env.crypto, 16);
  const redirectUri = redirectUriFor(env.origin);
  env.storage.setItem(PENDING_KEY, JSON.stringify({ state, nonce, verifier, redirectUri, silent: !!opts.silent, returnHash: opts.returnHash ?? env.hash, ts: env.now() }));
  env.assign(buildAuthorizationUrl(cfg, { state, nonce, challenge, redirectUri, silent: opts.silent }));
}

// ------------------------------------------------------------------ callback
export type ParsedCallback =
  | { kind: "none" }
  | { kind: "error"; error: string; description: string; state?: string; iss?: string }
  | { kind: "code"; code: string; state: string; iss?: string };

export function parseCallback(search: string): ParsedCallback {
  const q = new URLSearchParams(search);
  if (q.has("error")) return { kind: "error", error: q.get("error")!, description: q.get("error_description") ?? "", state: q.get("state") ?? undefined, iss: q.get("iss") ?? undefined };
  if (q.has("code")) return { kind: "code", code: q.get("code")!, state: q.get("state") ?? "", iss: q.get("iss") ?? undefined };
  return { kind: "none" };
}

export async function exchangeCode(cfg: OidcConfig, a: { code: string; verifier: string; redirectUri: string }, env: Pick<Env, "fetch" | "now">): Promise<{ tokens: TokenSet; idToken?: string }> {
  const body = new URLSearchParams({ grant_type: "authorization_code", code: a.code, redirect_uri: a.redirectUri, client_id: cfg.client_id, code_verifier: a.verifier });
  let res: Response;
  try {
    res = await env.fetch(cfg.token_endpoint, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body, credentials: "omit", cache: "no-store", redirect: "error" });
  } catch {
    throw new OidcClientError("Could not reach the identity provider's token endpoint.");
  }
  let json: Record<string, unknown> = {};
  try { json = await res.json(); } catch { /* handled below */ }
  if (!res.ok) throw new OidcClientError(`Sign-in was rejected: ${String(json.error ?? res.status)}${json.error_description ? ` (${String(json.error_description)})` : ""}`);
  if (typeof json.access_token !== "string" || !json.access_token) throw new OidcClientError("The identity provider returned no access token.");
  if (String(json.token_type ?? "").toLowerCase() !== "bearer") throw new OidcClientError("Unsupported token type.");
  const expiresIn = typeof json.expires_in === "number" ? json.expires_in : 300;
  // a refresh_token, if present, is deliberately dropped: nothing long-lived is kept in the browser
  return { tokens: { accessToken: json.access_token, idToken: typeof json.id_token === "string" ? json.id_token : undefined, expiresAt: env.now() + expiresIn * 1000 }, idToken: typeof json.id_token === "string" ? json.id_token : undefined };
}

export function decodeJwtPayload(jwt: string): Record<string, any> {
  const part = jwt.split(".")[1];
  if (!part) throw new OidcClientError("Malformed ID token.");
  const b64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
  const bin = atob(b64);
  return JSON.parse(new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0))));
}

export function validateIdToken(idToken: string, v: { issuer: string; clientId: string; nonce: string; nowMs: number }): void {
  const p = decodeJwtPayload(idToken);
  if (String(p.iss ?? "").replace(/\/$/, "") !== v.issuer.replace(/\/$/, "")) throw new OidcClientError("ID token issuer mismatch.");
  const aud = Array.isArray(p.aud) ? p.aud : [p.aud];
  if (!aud.includes(v.clientId)) throw new OidcClientError("ID token audience mismatch.");
  if (typeof p.exp !== "number" || p.exp * 1000 < v.nowMs - 60_000) throw new OidcClientError("ID token expired.");
  if (p.nonce !== v.nonce) throw new OidcClientError("ID token nonce mismatch (possible replay).");
}

export async function completeLogin(cfg: OidcConfig, env: Env): Promise<CallbackResult> {
  const cb = parseCallback(env.search);
  if (cb.kind === "none") return { status: "none" };

  // the code must not linger in the address bar, history or referrers
  env.replaceUrl(env.pathname + env.hash);

  const raw = env.storage.getItem(PENDING_KEY);
  env.storage.removeItem(PENDING_KEY); // single use, whatever happens next
  let pending: { state: string; nonce: string; verifier: string; redirectUri: string; silent: boolean; returnHash: string; ts: number } | null = null;
  try { pending = raw ? JSON.parse(raw) : null; } catch { pending = null; }
  const fail = (message: string, silentFailed = false): CallbackResult => ({ status: "error", message, silentFailed, returnHash: pending?.returnHash ?? "" });

  if (!pending) return fail("No sign-in was in progress in this browser tab. Please sign in again.");
  if (env.now() - pending.ts > PENDING_TTL_MS) return fail("The sign-in attempt took too long. Please try again.");
  if (cb.state !== pending.state) return fail("Sign-in response did not match this browser's request (state mismatch). Please try again.");
  if (cb.iss && cb.iss.replace(/\/$/, "") !== cfg.issuer.replace(/\/$/, "")) return fail("Sign-in response came from an unexpected issuer.");

  if (cb.kind === "error") {
    const silent = pending.silent && SILENT_ERRORS.has(cb.error);
    return fail(silent ? "Your single sign-on session has ended. Please sign in again." : `The identity provider refused the sign-in: ${cb.error}${cb.description ? ` (${cb.description})` : ""}`, silent);
  }

  try {
    const { tokens, idToken } = await exchangeCode(cfg, { code: cb.code, verifier: pending.verifier, redirectUri: pending.redirectUri }, env);
    if (idToken) validateIdToken(idToken, { issuer: cfg.issuer, clientId: cfg.client_id, nonce: pending.nonce, nowMs: env.now() });
    return { status: "ok", tokens, returnHash: pending.returnHash };
  } catch (e) {
    return fail(e instanceof Error ? e.message : "Sign-in failed.");
  }
}

export function endSessionUrl(cfg: OidcConfig, origin: string, idToken?: string): string | null {
  if (!cfg.end_session_endpoint) return null;
  const q = new URLSearchParams({ client_id: cfg.client_id, post_logout_redirect_uri: redirectUriFor(origin) });
  if (idToken) q.set("id_token_hint", idToken);
  return `${cfg.end_session_endpoint}${cfg.end_session_endpoint.includes("?") ? "&" : "?"}${q.toString()}`;
}
