// The API from a spec (plan M4 Task 32): a request context that sends what the SPA sends, and the saved session
// of the smoke user. The session cookie is `__Host-` and `Secure`; Playwright's request contexts send a Secure
// cookie to localhost over plain HTTP, as the browser does. Task 33 adds the page's answer to one of its own
// requests, and a CV draft's path and state. Every request context of the harness is made by `newApiContext`
// (Ruling F4-c; eslint.config.js forbids `request.newContext` and `page.request` elsewhere): its requests'
// failures are rethrown redacted (`redact.ts`), and the cookies it starts with are remembered as secrets.
import { type APIRequestContext, test as base, type Page, request, type Response } from "@playwright/test";
import type { components } from "../../src/api/schema";
import { AUTH_STATE } from "./paths";
import { rememberSecret, scrubbed } from "./redact";
import { type SmokeUser, waitFor } from "./stack";

type DraftOut = components["schemas"]["DraftOut"];

/** The dev stack's one origin (devproxy). */
export const ORIGIN = "http://localhost:8080";
/** The marker the edge's CSRF check needs on a request that changes state, with the `Origin` (§16.5). */
export const API_HEADERS: Readonly<Record<string, string>> = { Origin: ORIGIN, "X-JH-Request": "1" };

export type ApiContext = APIRequestContext & {
  /** A new `Idempotency-Key` (one per user action, §14.1). */
  key(): string;
};

type ContextOptions = NonNullable<Parameters<typeof request.newContext>[0]>;
// The members of a request context that send a request or read its cookies: each failure is rethrown redacted.
const SENDING: ReadonlySet<string | symbol> = new Set(["fetch", "get", "post", "put", "patch", "delete", "head", "storageState", "dispose"]);
const METHODS: Readonly<Partial<Record<string, string>>> = { get: "GET", post: "POST", put: "PUT", patch: "PATCH", delete: "DELETE", head: "HEAD" };

/** Playwright's own wrapper of a call (no public API): a call made inside an internal one is recorded as no step
 * of the test, so the step that would keep the raw error, its call log and the request's cookie header in it,
 * in the HTML report is never made. `newApiContext` records a step of its own with the redacted error. */
interface InternalCalls {
  _wrapApiCall<T>(work: () => Promise<T>, options: { internal: boolean }): Promise<T>;
}

/** The step title of a request (`POST "/api/settings"`, as Playwright titles one); `null` for no request. */
function stepTitle(member: string | symbol, args: readonly unknown[]): string | null {
  const [target, options] = args;
  const asked = (options as { method?: unknown } | undefined)?.method;
  const method = member === "fetch" ? (typeof asked === "string" ? asked.toUpperCase() : "GET") : METHODS[String(member)];
  if (method === undefined || typeof target !== "string") {
    return null;
  }
  const url = new URL(target, ORIGIN);
  return `${method} "${url.pathname}${url.search}"`;
}

/** Whether a test is running (`test.step` needs one; the global setup runs outside any). */
function inTest(): boolean {
  try {
    base.info();
    return true;
  } catch {
    return false;
  }
}

/** Remembers the values of `context`'s cookies as secrets (`rememberSecret`). */
async function rememberCookies(context: APIRequestContext): Promise<void> {
  for (const cookie of (await context.storageState()).cookies) {
    rememberSecret(cookie.value);
  }
}

/** A request context (`request.newContext(options)`) whose requests' failures are rethrown redacted
 * (`redact.ts`): no session cookie of its call log, no password, reaches an error message, a reporter or the
 * report. The cookies it starts with are remembered as secrets; the caller disposes it. */
export async function newApiContext(options: ContextOptions): Promise<APIRequestContext> {
  const context = await scrubbed(() => request.newContext(options));
  await scrubbed(() => rememberCookies(context));
  const internal = context as unknown as InternalCalls;
  return new Proxy(context, {
    get(target, property) {
      const member: unknown = Reflect.get(target, property, target);
      if (typeof member !== "function" || !SENDING.has(property)) {
        return member;
      }
      const send = member as (...args: unknown[]) => Promise<unknown>;
      return (...args: unknown[]) => {
        const call = () => scrubbed(() => internal._wrapApiCall(() => send.apply(target, args), { internal: true }));
        const title = stepTitle(property, args);
        return title !== null && inTest() ? base.step(title, call, { box: true }) : call();
      };
    },
  });
}

/** A request context with the page's cookies, `Origin` and `X-JH-Request: 1` (`newApiContext`); the caller
 * disposes it. */
export async function apiContext(page: Page): Promise<ApiContext> {
  const context = await newApiContext({
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

/** How long a draft's server step may take: the text phase (docparse), or the extraction on the fake LLM. */
export const DRAFT_WITHIN_MS = 120_000;

/** The answer to the page's next `method` request of `path` (its pathname: any search); start waiting before
 * the action that sends it. */
export function answerTo(page: Page, method: string, path: string): Promise<Response> {
  return page.waitForResponse(
    (response) => response.request().method() === method && new URL(response.url()).pathname === path,
  );
}

/** A CV draft's API path (`/api/profile/drafts/{id}`), with `rest` after it (`/text`, `/confirm`, …). */
export function draftPath(id: number, rest = ""): string {
  return `/api/profile/drafts/${String(id)}${rest}`;
}

/** Draft `id` through the API once `done` holds for it, polled within `timeoutMs`. A draft that is `failed`
 * while `done` does not hold fails at once, naming its failure code and `what` was awaited. */
export async function draftWhen(
  api: APIRequestContext,
  id: number,
  what: string,
  done: (draft: DraftOut) => boolean,
  timeoutMs = DRAFT_WITHIN_MS,
): Promise<DraftOut> {
  return waitFor(
    async () => {
      const draft = await getJson<DraftOut>(api, draftPath(id));
      if (done(draft)) {
        return draft;
      }
      if (draft.status === "failed") {
        throw new Error(`draft ${String(id)} failed (${String(draft.failure_code)}) before ${what}`);
      }
      return null;
    },
    timeoutMs,
    `draft ${String(id)}: ${what}`,
  );
}

/** The smoke user signed in through the API, its session saved to `AUTH_STATE` (what `scenarios` start on). */
export async function saveAuthState(user: SmokeUser): Promise<void> {
  rememberSecret(user.password);
  const context = await newApiContext({ baseURL: ORIGIN });
  try {
    const login = await context.post("/api/auth/login", {
      data: { username: user.username, password: user.password },
      headers: { Origin: ORIGIN },
    });
    if (login.status() !== 204) {
      throw new Error(`the API login of ${user.username} answered ${String(login.status())}, not 204`);
    }
    await rememberCookies(context);
    await context.storageState({ path: AUTH_STATE });
  } finally {
    await context.dispose();
  }
}
