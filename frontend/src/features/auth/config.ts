/** Google OAuth + auth-API configuration (Vite env, VITE_* prefix).
 *
 *  Sign-in is Google-only: the frontend gets a Google ID token from Google
 *  Identity Services and POSTs it to the auth backend, which validates it and
 *  returns a session. This mirrors the reference auth app.
 */

// The Google OAuth client the ID token is minted for. Falls back to the shared
// Deccan client id so sign-in works out of the box in dev; override per
// environment via VITE_GOOGLE_CLIENT_ID.
export const GOOGLE_CLIENT_ID =
  (import.meta.env.VITE_GOOGLE_CLIENT_ID as string | undefined)?.trim() ||
  "908367891804-cohtn6o8akmjdubh1gp4fgbq49ra3ldl.apps.googleusercontent.com";

// Base URL of the auth backend that exchanges a Google ID token for a session.
// The credential is POSTed to `${AUTH_ROOT}${SIGNIN_PATH}`. Set via VITE_ROOT.
export const AUTH_ROOT = (import.meta.env.VITE_ROOT as string | undefined)?.trim() || "";

// The exchange endpoint on the auth backend (reference: /auth/v2/signin).
export const SIGNIN_PATH = "/auth/v2/signin";

// Authenticated user-details endpoint. Used to VALIDATE a session token (a 200
// with a Bearer token means the token is live) and to enrich the profile.
export const DETAILS_PATH = "/v1/user/details";

// Issuer identifier sent in the sign-in request body (reference: "platform").
export const AUTH_ISSUER = (import.meta.env.VITE_ISSUER as string | undefined)?.trim() || "platform";
