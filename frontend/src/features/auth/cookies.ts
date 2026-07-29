/** Cookie helpers for the auth session (via js-cookie, as in the reference auth
 *  app). Host-only with no `secure` on localhost/http; on a deployed https host,
 *  `secure` + an optional shared parent domain (VITE_DOMAIN) so sibling apps on
 *  the same domain can read the session. */

import Cookies from "js-cookie";

const isLocalHost = (h: string): boolean =>
  h === "localhost" || h === "127.0.0.1" || h.endsWith(".local");

function cookieAttrs(maxAgeSeconds?: number): Cookies.CookieAttributes {
  const attrs: Cookies.CookieAttributes = { path: "/", sameSite: "lax" };
  if (typeof window !== "undefined") {
    const { hostname, protocol } = window.location;
    const domain = (import.meta.env.VITE_DOMAIN as string | undefined)?.trim();
    if (!isLocalHost(hostname) && domain) attrs.domain = domain;
    if (protocol === "https:") attrs.secure = true;
  }
  // js-cookie expresses lifetime in days; a session cookie (undefined) is dropped on close.
  if (maxAgeSeconds != null) attrs.expires = maxAgeSeconds / 86400;
  return attrs;
}

export function setCookie(name: string, value: string, maxAgeSeconds?: number): void {
  Cookies.set(name, value, cookieAttrs(maxAgeSeconds));
}

export function getCookie(name: string): string | null {
  return Cookies.get(name) ?? null;
}

export function removeCookie(name: string): void {
  // Remove with matching path/domain so the deployed shared-domain cookie is cleared too.
  Cookies.remove(name, cookieAttrs());
}
