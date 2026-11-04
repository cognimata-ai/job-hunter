// The typed wrapper around `fetch` for the api (spec §14.4, §14.5, §15.8, §16.5).
import type { components } from "./schema";

export type FieldError = components["schemas"]["FieldError"];

const PROBLEM_MEDIA_TYPE = "application/problem+json";
const CSRF_HEADER = "X-JH-Request";
const BACKGROUND_HEADER = "X-JH-Background";
const IDEMPOTENCY_HEADER = "Idempotency-Key";
const REQUEST_ID_HEADER = "X-Request-Id";
const STATE_CHANGING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
// The members a problem document writes itself (the backend's RESERVED_PROBLEM_MEMBERS); every other member is
// an extension (`retry_after_s`, `merged_into_job_id`, `retryable`, `query_id`, …), kept in `extra` (D29).
const RESERVED_MEMBERS = new Set(["type", "title", "status", "code", "detail", "instance", "request_id", "errors"]);
/** The code of a request that got no answer at all (`toProblem`): no server code, `status` 0. */
export const NETWORK_CODE = "network";

/** An error response of the api (RFC 9457 problem details). `message` is the problem's `title`. */
export class ProblemError extends Error {
  readonly status: number;
  /** The problem's stable `code`; `"internal"` when the response was not a problem document. */
  readonly code: string;
  readonly requestId: string | null;
  readonly errors: FieldError[];
  readonly detail: string | null;
  readonly instance: string | null;
  /** The problem's extension members, by name. */
  readonly extra: Record<string, unknown>;

  constructor(init: {
    status: number;
    code: string;
    title: string;
    requestId: string | null;
    errors: FieldError[];
    detail?: string | null;
    instance?: string | null;
    extra?: Record<string, unknown>;
  }) {
    super(init.title);
    this.name = "ProblemError";
    this.status = init.status;
    this.code = init.code;
    this.requestId = init.requestId;
    this.errors = init.errors;
    this.detail = init.detail ?? null;
    this.instance = init.instance ?? null;
    this.extra = init.extra ?? {};
  }
}

/** Whether `e` is a `ProblemError`, and of `code` when one is given. */
export function isProblem(e: unknown, code?: string): e is ProblemError {
  return e instanceof ProblemError && (code === undefined || e.code === code);
}

/** `e` as a `ProblemError`: itself, or for any other failure (no answer: offline, a network error) the
 * problem of code `"network"` with status 0. */
export function toProblem(e: unknown): ProblemError {
  if (e instanceof ProblemError) {
    return e;
  }
  return new ProblemError({
    status: 0,
    code: NETWORK_CODE,
    title: e instanceof Error ? e.message : "No answer from the server",
    requestId: null,
    errors: [],
  });
}

/** `fetch`'s init, and what the api adds: the poll marker (§15, D28) and the key of a keyed write (D29). */
export interface ApiInit extends RequestInit {
  /** A poll: `X-JH-Background: 1`, so the request never refreshes the session's idle timer. */
  background?: boolean;
  /** Sent as `Idempotency-Key` (`api/idempotency.ts` makes one per user action). */
  idempotencyKey?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFieldError(value: unknown): value is FieldError {
  return (
    isRecord(value) &&
    typeof value.field === "string" &&
    typeof value.code === "string" &&
    typeof value.message === "string"
  );
}

function isProblemResponse(response: Response): boolean {
  const mediaType = (response.headers.get("content-type") ?? "").split(";")[0] ?? "";
  return mediaType.trim().toLowerCase() === PROBLEM_MEDIA_TYPE;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined; // an unparsable body is "not a problem document", not a second error
  }
}

async function problemFrom(response: Response): Promise<ProblemError> {
  const headerRequestId = response.headers.get(REQUEST_ID_HEADER);
  const body = isProblemResponse(response) ? await readJson(response) : undefined;
  if (!isRecord(body) || typeof body.code !== "string") {
    return new ProblemError({
      status: response.status,
      code: "internal",
      title: response.statusText || `HTTP ${String(response.status)}`,
      requestId: headerRequestId,
      errors: [],
    });
  }
  return new ProblemError({
    status: response.status,
    code: body.code,
    title: typeof body.title === "string" ? body.title : `HTTP ${String(response.status)}`,
    requestId: typeof body.request_id === "string" ? body.request_id : headerRequestId,
    errors: Array.isArray(body.errors) ? body.errors.filter(isFieldError) : [],
    detail: typeof body.detail === "string" ? body.detail : null,
    instance: typeof body.instance === "string" ? body.instance : null,
    extra: Object.fromEntries(Object.entries(body).filter(([name]) => !RESERVED_MEMBERS.has(name))),
  });
}

/**
 * Calls the api at `path` and returns its JSON body (`undefined` for a 204 or any other empty 2xx body; use
 * `apiFetch<undefined>`).
 *
 * Cookies go with same-origin requests only. State-changing methods carry `X-JH-Request: 1`, which the edge
 * requires (§16.5); a poll (`background`) carries `X-JH-Background: 1`, and `idempotencyKey` goes as
 * `Idempotency-Key`. A string body is sent as JSON unless the caller set a content type. Any non-2xx response,
 * and a 2xx whose body is not JSON (code `"internal"`), throws a `ProblemError`; a network failure rejects with
 * `fetch`'s own error (`toProblem` makes it the `"network"` problem).
 */
export async function apiFetch<T>(path: string, init: ApiInit = {}): Promise<T> {
  const { background, idempotencyKey, ...requestInit } = init;
  const headers = new Headers(requestInit.headers);
  if (STATE_CHANGING_METHODS.has((requestInit.method ?? "GET").toUpperCase())) {
    headers.set(CSRF_HEADER, "1");
  }
  if (background === true) {
    headers.set(BACKGROUND_HEADER, "1");
  }
  if (idempotencyKey !== undefined) {
    headers.set(IDEMPOTENCY_HEADER, idempotencyKey);
  }
  if (typeof requestInit.body === "string" && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  const response = await fetch(path, { ...requestInit, headers, credentials: "same-origin" });
  if (!response.ok) {
    throw await problemFrom(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  if (text === "") {
    return undefined as T;
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ProblemError({
      status: response.status,
      code: "internal",
      title: "The response body is not JSON",
      requestId: response.headers.get(REQUEST_ID_HEADER),
      errors: [],
    });
  }
}

/** `apiFetch` with every failure a `ProblemError` (`toProblem`: no answer is the `"network"` problem), for a
 * caller that shows or branches on the problem. */
export async function apiSend<T>(path: string, init: ApiInit = {}): Promise<T> {
  try {
    return await apiFetch<T>(path, init);
  } catch (error) {
    throw toProblem(error);
  }
}
