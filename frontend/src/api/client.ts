// The typed wrapper around `fetch` for the api (spec §14.4, §14.5, §16.5).
import type { components } from "./schema";

export type FieldError = components["schemas"]["FieldError"];

const PROBLEM_MEDIA_TYPE = "application/problem+json";
const CSRF_HEADER = "X-JH-Request";
const REQUEST_ID_HEADER = "X-Request-Id";
const STATE_CHANGING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** An error response of the api (RFC 9457 problem details). `message` is the problem's `title`. */
export class ProblemError extends Error {
  readonly status: number;
  /** The problem's stable `code`; `"internal"` when the response was not a problem document. */
  readonly code: string;
  readonly requestId: string | null;
  readonly errors: FieldError[];

  constructor(init: {
    status: number;
    code: string;
    title: string;
    requestId: string | null;
    errors: FieldError[];
  }) {
    super(init.title);
    this.name = "ProblemError";
    this.status = init.status;
    this.code = init.code;
    this.requestId = init.requestId;
    this.errors = init.errors;
  }
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
  });
}

/**
 * Calls the api at `path` and returns its JSON body (`undefined` for a 204 or any other empty 2xx body; use
 * `apiFetch<undefined>`).
 *
 * Cookies go with same-origin requests only. State-changing methods carry `X-JH-Request: 1`, which the edge
 * requires (§16.5); a string body is sent as JSON unless the caller set a content type. Any non-2xx response,
 * and a 2xx whose body is not JSON (code `"internal"`), throws a `ProblemError`; a network failure rejects with
 * `fetch`'s own error.
 */
export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (STATE_CHANGING_METHODS.has((init.method ?? "GET").toUpperCase())) {
    headers.set(CSRF_HEADER, "1");
  }
  if (typeof init.body === "string" && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
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
