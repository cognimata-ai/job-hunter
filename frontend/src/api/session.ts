// The session (spec §15.8, §16.4, E19): the session read and the way back after a login.
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { apiFetch } from "./client";
import type { components } from "./schema";

export type SessionOut = components["schemas"]["SessionOut"];

export const LOGIN_PATH = "/login";
export const SESSION_KEY = ["session"] as const;
const BASE = "http://same-origin.invalid";

/** The current session (`GET /api/auth/session`); its 401 is an answer (`authCall`), never an expiry. */
export function useSession(): UseQueryResult<SessionOut> {
  return useQuery({
    queryKey: SESSION_KEY,
    queryFn: ({ signal }) => apiFetch<SessionOut>("/api/auth/session", { signal }),
    meta: { authCall: true },
  });
}

/** Whether `text` has a C0 control character or DEL, which a browser strips from a URL. */
function hasControl(text: string): boolean {
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) {
      return true;
    }
  }
  return false;
}

/** Whether `pathname` (as the router has it, decoded) is the login page's: the router matches paths without
 * regard to case, and a trailing slash is the same path. */
export function isLoginPath(pathname: string): boolean {
  return pathname.toLowerCase().replace(/\/+$/, "") === LOGIN_PATH;
}

function decodedPath(path: string): string | null {
  try {
    return decodeURIComponent(path);
  } catch {
    return null; // malformed percent-encoding
  }
}

/**
 * Where a login may send the user: `raw` when it is a path of this origin (one leading `/`, no backslash, no
 * control character, which a browser would strip into `//`), also once decoded as the router decodes it (so
 * `/%2F%2Fevil.example` is refused), and not the login page itself in any case (`/LOGIN`); else `/`.
 */
export function safeRedirect(raw: string | undefined): string {
  if (raw === undefined || !raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\") || hasControl(raw)) {
    return "/";
  }
  const url = new URL(raw, BASE);
  const path = decodedPath(url.pathname);
  if (url.origin !== BASE || path === null || path.startsWith("//") || path.includes("\\") || hasControl(path)) {
    return "/";
  }
  if (isLoginPath(path) || path.toLowerCase().startsWith(`${LOGIN_PATH}/`)) {
    return "/";
  }
  return `${url.pathname}${url.search}${url.hash}`;
}
