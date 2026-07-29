/** Auth API — Google sign-in + token hand-off.
 *
 *  Two ways to obtain a session:
 *   1. Google sign-in: Google Identity Services returns an ID token
 *      (`credential`); we POST it to `${AUTH_ROOT}${SIGNIN_PATH}` (token in the
 *      `token` header). The backend returns access + refresh tokens.
 *   2. URL hand-off: another app can redirect here with `?token=…&refreshToken=…`.
 *      We VALIDATE the token by calling `${AUTH_ROOT}${DETAILS_PATH}` with
 *      `Authorization: Bearer <token>`; a 200 means it is live.
 *
 *  Either way the tokens are persisted in cookies and the signed-in identity is
 *  derived from the session token's JWT claims (`data.email`, `data.user_id`),
 *  enriched by the user-details response when available.
 */

import { AUTH_ISSUER, AUTH_ROOT, DETAILS_PATH, SIGNIN_PATH } from "./config";
import { getCookie, removeCookie, setCookie } from "./cookies";

export interface AnnotatorStats {
  sessions: number;
  submitted: number;
  golden: number;
  breaker: number;
  flagged: number;
}

export interface Annotator {
  id: string;
  email: string;
  role: string;
  displayName: string;
  avatarHue: number;
  lastLoginAt: string | null;
  stats?: AnnotatorStats;
}

export type LoginResult = { ok: true; annotator: Annotator } | { ok: false; error: string };

// Session cookie names (reference-compatible).
const TOKEN = "token";
const REFRESH_TOKEN = "refreshToken";
const STATUS = "status";
const FIRST_LOGIN = "firstLogin";
const EMAIL = "email";
// The rendered identity is kept in localStorage so a reload restores the
// display name/avatar without a round-trip.
const PROFILE_KEY = "bg_annotator_profile";
const WEEK_SECONDS = 7 * 24 * 60 * 60;

// ---- JWT / claim helpers ---------------------------------------------------

function str(v: unknown): string | undefined {
  return typeof v === "string" && v.trim() ? v : undefined;
}

/** Decode a JWT payload without a dependency. Best-effort — a malformed token
 *  yields {} and callers fall back to other sources. */
function decodeJwtPayload(jwt: string): Record<string, unknown> {
  try {
    const seg = jwt.split(".")[1];
    const b64 = seg.replace(/-/g, "+").replace(/_/g, "/");
    const json = decodeURIComponent(
      atob(b64)
        .split("")
        .map((c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0"))
        .join(""),
    );
    return JSON.parse(json) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/** Google ID token claims (email/name at the top level). */
function googleClaims(credential: string): { email?: string; name?: string } {
  const p = decodeJwtPayload(credential);
  return { email: str(p.email), name: str(p.name) };
}

/** Platform session token claims — payload is `{ data: { email, user_id, … } }`. */
function sessionClaims(token: string): { email?: string; userId?: string } {
  const p = decodeJwtPayload(token);
  const data = p.data && typeof p.data === "object" ? (p.data as Record<string, unknown>) : {};
  return { email: str(data.email) ?? str(p.email), userId: str(data.user_id) ?? str(p.user_id) };
}

// ---- identity building -----------------------------------------------------

/** Deterministic 0–359 hue from the email so an avatar color is stable per user. */
function hueFromEmail(email: string): number {
  let h = 0;
  for (let i = 0; i < email.length; i++) h = (h * 31 + email.charCodeAt(i)) >>> 0;
  return h % 360;
}

/** Read a field from the user-details response, checking both the top level and
 *  a nested `data` object (backends differ in envelope shape). */
function fromDetails(details: Record<string, unknown> | null, keys: string[]): string | undefined {
  if (!details) return undefined;
  const data = details.data && typeof details.data === "object" ? (details.data as Record<string, unknown>) : {};
  for (const k of keys) {
    const v = str(details[k]) ?? str(data[k]);
    if (v) return v;
  }
  return undefined;
}

function joinName(details: Record<string, unknown> | null): string | undefined {
  const first = fromDetails(details, ["first_name", "firstName", "given_name"]);
  const last = fromDetails(details, ["last_name", "lastName", "family_name"]);
  const full = [first, last].filter(Boolean).join(" ").trim();
  return full || undefined;
}

function buildAnnotator(email: string, opts: { name?: string; id?: string } = {}): Annotator {
  const displayName = opts.name?.trim() || (email ? email.split("@")[0] : "annotator");
  return {
    id: opts.id || email || displayName,
    email,
    role: "annotator",
    displayName,
    avatarHue: hueFromEmail(email || displayName),
    lastLoginAt: new Date().toISOString(),
  };
}

function annotatorFromToken(token: string, details: Record<string, unknown> | null): Annotator {
  const claims = sessionClaims(token);
  const email = (fromDetails(details, ["email"]) || claims.email || "").toLowerCase();
  const name = fromDetails(details, ["name", "full_name", "display_name"]) || joinName(details);
  const id = fromDetails(details, ["user_id", "id", "uuid"]) || claims.userId;
  return buildAnnotator(email, { name, id });
}

function persistProfile(annotator: Annotator): void {
  try {
    localStorage.setItem(PROFILE_KEY, JSON.stringify(annotator));
  } catch {
    /* storage may be unavailable (private mode) — cookie session still holds */
  }
}

// ---- backend calls ---------------------------------------------------------

/** GET the authenticated user's details. Returns the parsed body on 200, else
 *  null (used both to validate a token and to enrich the profile). */
export async function fetchUserDetails(token: string): Promise<Record<string, unknown> | null> {
  if (!AUTH_ROOT || !token) return null;
  try {
    const res = await fetch(`${AUTH_ROOT}${DETAILS_PATH}`, {
      headers: { accept: "application/json, text/plain, */*", authorization: `Bearer ${token}` },
    });
    if (!res.ok) return null;
    return (await res.json().catch(() => ({}))) as Record<string, unknown>;
  } catch {
    return null;
  }
}

interface SigninData {
  access_token?: string;
  refresh_token?: string;
  status?: string;
  first_login?: boolean;
  email?: string;
}
interface SigninResponse {
  message?: string;
  data?: SigninData;
}

/** Exchange a Google ID token for a session (login button). */
export async function signInWithGoogle(credential: string, staySignedIn = false): Promise<LoginResult> {
  if (!credential) return { ok: false, error: "No credential returned by Google." };
  if (!AUTH_ROOT) return { ok: false, error: "Auth API is not configured — set VITE_ROOT." };

  const claims = googleClaims(credential);
  try {
    const res = await fetch(`${AUTH_ROOT}${SIGNIN_PATH}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-request-id": `auth-${Date.now()}`,
        token: credential,
      },
      credentials: "include",
      body: JSON.stringify({ issuer: AUTH_ISSUER, stay_signed_in: staySignedIn }),
    });
    const payload = (await res.json().catch(() => ({}))) as SigninResponse;
    if (!res.ok) return { ok: false, error: payload.message || `Sign-in failed (${res.status})` };

    const data = payload.data ?? {};
    if (!data.access_token || !data.refresh_token) {
      return { ok: false, error: payload.message || "Sign-in did not return a session." };
    }

    const maxAge = staySignedIn ? WEEK_SECONDS : undefined;
    setCookie(TOKEN, data.access_token, maxAge);
    setCookie(REFRESH_TOKEN, data.refresh_token, maxAge);
    if (data.status) setCookie(STATUS, data.status, maxAge);
    if (data.first_login != null) setCookie(FIRST_LOGIN, String(data.first_login), maxAge);

    const email = (data.email || claims.email || "").toLowerCase();
    if (email) setCookie(EMAIL, email, maxAge);

    const annotator = buildAnnotator(email, { name: claims.name });
    persistProfile(annotator);
    return { ok: true, annotator };
  } catch {
    return { ok: false, error: "Cannot reach the sign-in server." };
  }
}

/** Sign in from tokens handed off by another app. The token is VALIDATED against
 *  the user-details endpoint before anything is stored. */
export async function signInWithTokens(
  token: string,
  refreshToken?: string,
  staySignedIn = true,
): Promise<LoginResult> {
  if (!token) return { ok: false, error: "Missing token." };
  if (!AUTH_ROOT) return { ok: false, error: "Auth API is not configured — set VITE_ROOT." };

  const details = await fetchUserDetails(token);
  if (!details) return { ok: false, error: "Token is invalid or expired." };

  const maxAge = staySignedIn ? WEEK_SECONDS : undefined;
  setCookie(TOKEN, token, maxAge);
  if (refreshToken) setCookie(REFRESH_TOKEN, refreshToken, maxAge);

  const annotator = annotatorFromToken(token, details);
  if (annotator.email) setCookie(EMAIL, annotator.email, maxAge);
  persistProfile(annotator);
  return { ok: true, annotator };
}

// ---- session resolution ----------------------------------------------------

/** Consume `?token=…&refreshToken=…` from the URL, stripping them from the
 *  address bar / history so they don't linger. Returns null when absent. */
function readTokensFromUrl(): { token: string; refreshToken?: string } | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  const token = params.get("token");
  if (!token) return null;
  const refreshToken = params.get("refreshToken") ?? undefined;

  params.delete("token");
  params.delete("refreshToken");
  const qs = params.toString();
  const cleaned = window.location.pathname + (qs ? `?${qs}` : "") + window.location.hash;
  window.history.replaceState({}, document.title, cleaned);

  return { token, refreshToken };
}

/** Trust-only restore from local storage (used when the auth API is not
 *  configured, e.g. a pure-local dev run). */
function restoreFromStore(): Annotator | null {
  try {
    const raw = localStorage.getItem(PROFILE_KEY);
    if (raw) return JSON.parse(raw) as Annotator;
  } catch {
    /* fall through */
  }
  const email = getCookie(EMAIL);
  return email ? buildAnnotator(email) : null;
}

/** Resolve the current session on app load:
 *   1. token in the URL → validate → store → sign in,
 *   2. else a token cookie → validate against the backend (drop it if stale),
 *   3. else no session.
 *  When VITE_ROOT is unset we cannot validate, so we trust the stored session. */
export async function resolveSession(): Promise<Annotator | null> {
  const handoff = readTokensFromUrl();
  if (handoff) {
    const r = await signInWithTokens(handoff.token, handoff.refreshToken);
    return r.ok ? r.annotator : null;
  }

  const token = getCookie(TOKEN);
  if (!token) return null;

  if (!AUTH_ROOT) return restoreFromStore();

  const details = await fetchUserDetails(token);
  if (!details) {
    logout();
    return null;
  }
  const annotator = annotatorFromToken(token, details);
  persistProfile(annotator);
  return annotator;
}

/** Clear the local session (Google sign-out is handled by GIS on next prompt). */
export function logout(): void {
  [TOKEN, REFRESH_TOKEN, STATUS, FIRST_LOGIN, EMAIL].forEach(removeCookie);
  try {
    localStorage.removeItem(PROFILE_KEY);
  } catch {
    /* ignore */
  }
}
