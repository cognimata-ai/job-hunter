// The API from a spec (plan M4 Task 32): a request context that sends what the SPA sends, and the saved session
// of the smoke user. The session cookie is `__Host-` and `Secure`; Playwright's request contexts send a Secure
// cookie to localhost over plain HTTP, as the browser does.
import { type APIRequestContext, type Page, request } from "@playwright/test";
import { AUTH_STATE } from "./paths";
import type { SmokeUser } from "./stack";

/** The dev stack's one origin (devproxy). */
export const ORIGIN = "http://localhost:8080";
/** The marker the edge's CSRF check needs on a request that changes state, with the `Origin` (§16.5). */
export const API_HEADERS: Readonly<Record<string, string>> = { Origin: ORIGIN, "X-JH-Request": "1" };

export type ApiContext = APIRequestContext & {
  /** A new `Idempotency-Key` (one per user action, §14.1). */
  key(): string;
};

/** A request context with the page's cookies, `Origin` and `X-JH-Request: 1`; the caller disposes it. */
export async function apiContext(page: Page): Promise<ApiContext> {
  const context = await request.newContext({
    baseURL: ORIGIN,
    storageState: await page.context().storageState(),
    extraHTTPHeaders: { ...API_HEADERS },
  });
  return Object.assign(context, { key: () => crypto.randomUUID() });
}

/** `GET path` through `api`: its JSON, typed by the caller (a `components["schemas"]` type); any status but 200
 * throws with the path and the status, never the body. */
export async function getJson<T>(api: APIRequestContext, path: string): Promise<T> {
  const response = await api.get(path);
  if (response.status() !== 200) {
    throw new Error(`GET ${path} answered ${String(response.status())}, not 200`);
  }
  return (await response.json()) as T;
}

/** The smoke user signed in through the API, its session saved to `AUTH_STATE` (what `scenarios` start on). */
export async function saveAuthState(user: SmokeUser): Promise<void> {
  const context = await request.newContext({ baseURL: ORIGIN });
  try {
    const login = await context.post("/api/auth/login", {
      data: { username: user.username, password: user.password },
      headers: { Origin: ORIGIN },
    });
    if (login.status() !== 204) {
      throw new Error(`the API login of ${user.username} answered ${String(login.status())}, not 204`);
    }
    await context.storageState({ path: AUTH_STATE });
  } finally {
    await context.dispose();
  }
}
