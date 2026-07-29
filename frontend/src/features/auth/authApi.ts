/** Auth API — Google sign-in only.
 *
 *  Flow (mirrors the reference auth app): Google Identity Services returns an ID
 *  token (`credential`); we POST it to `${AUTH_ROOT}${SIGNIN_PATH}` with the
 *  token in the `token` header. The backend validates it and returns a session
 *  (access + refresh tokens), which we persist in cookies. The signed-in
 *  identity is derived from the Google token / backend response so the app can
 *  render the annotator without a second round-trip.
 */

import { AUTH_ISSUER, AUTH_ROOT, SIGNIN_PATH } from "./config";
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
// display name/avatar without decoding the token again.
const PROFILE_KEY = "bg_annotator_profile";
const WEEK_SECONDS = 7 * 24 * 60 * 60;

interface GoogleClaims {
  email?: string;
  name?: string;
  picture?: string;
  sub?: string;
}

/** Decode a JWT payload (Google ID token) without a dependency. Best-effort — a
 *  malformed token yields {} and the caller falls back to the backend response. */
function decodeJwt(jwt: string): GoogleClaims {
  try {
    const payload = jwt.split(".")[1];
    const b64 = payload.replace(/-/g, "+").replace(/_/g, "/");
    const json = decodeURIComponent(
      atob(b64)
        .split("")
        .map((c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0"))
        .join(""),
    );
    return JSON.parse(json) as GoogleClaims;
  } catch {
    return {};
  }
}

/** Deterministic 0–359 hue from the email so an avatar color is stable per user. */
function hueFromEmail(email: string): number {
  let h = 0;
  for (let i = 0; i < email.length; i++) h = (h * 31 + email.charCodeAt(i)) >>> 0;
  return h % 360;
}

function buildAnnotator(email: string, name?: string): Annotator {
  const displayName = (name && name.trim()) || (email ? email.split("@")[0] : "annotator");
  return {
    id: email || displayName,
    email,
    role: "annotator",
    displayName,
    avatarHue: hueFromEmail(email || displayName),
    lastLoginAt: new Date().toISOString(),
  };
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

/** Exchange a Google ID token for a session. On success the tokens are stored in
 *  cookies and the signed-in annotator is returned for the app to render. */
export async function signInWithGoogle(credential: string, staySignedIn = false): Promise<LoginResult> {
  if (!credential) return { ok: false, error: "No credential returned by Google." };
  if (!AUTH_ROOT) return { ok: false, error: "Auth API is not configured — set VITE_ROOT." };

  const claims = decodeJwt(credential);
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

    const annotator = buildAnnotator(email, claims.name);
    try {
      localStorage.setItem(PROFILE_KEY, JSON.stringify(annotator));
    } catch {
      /* storage may be unavailable (private mode) — cookie session still holds */
    }
    return { ok: true, annotator };
  } catch {
    return { ok: false, error: "Cannot reach the sign-in server." };
  }
}

/** Restore a session on load: present iff the session cookie is set. */
export function restoreSession(): Annotator | null {
  if (!getCookie(TOKEN)) return null;
  try {
    const raw = localStorage.getItem(PROFILE_KEY);
    if (raw) return JSON.parse(raw) as Annotator;
  } catch {
    /* fall through to rebuild from the email cookie */
  }
  const email = getCookie(EMAIL);
  return email ? buildAnnotator(email) : null;
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
