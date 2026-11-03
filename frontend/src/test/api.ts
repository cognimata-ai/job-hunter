// A stub of the api behind `fetch`, for tests that render the app: a session that a login opens and a test
// can end, the session, settings and header-status reads, the logout, the login's answers in the order a test
// gives them, and any other route a test answers itself (`routes`).
import { vi } from "vitest";
import type { components } from "../api/schema";

type SessionOut = components["schemas"]["SessionOut"];
type StatusOut = components["schemas"]["StatusOut"];

export const SESSION: SessionOut = {
  username: "smoke",
  totp_enabled: false,
  idle_expires_at: "2026-11-03T12:30:00Z",
  absolute_expires_at: "2026-11-04T12:00:00Z",
};

export function settingsRead(uiLanguage: "en" | "de" = "en", timezone = "Europe/Berlin") {
  return {
    version: { id: 41, version_no: 12, created_at: "2026-11-02T10:00:00Z", effect_classes: [] },
    payload: { display: { ui_language: uiLanguage, timezone } },
    pending_scope: null,
    scope_applied: true,
  };
}

/** The status at 07:00 UTC on 3 Nov 2026 (08:00 in Berlin): a partial run queued two hours before, the next
 * slot at 13:00 UTC (14:00 in Berlin), and nothing paused (spec §15.1's example strip). */
export function statusOut(overrides: Partial<StatusOut> = {}): StatusOut {
  return {
    last_run: {
      run: {
        id: 7,
        kind: "collect",
        trigger: "scheduled",
        status: "partial",
        status_reason: "fetch_failed",
        scheduled_slot: "2026-11-03T05:00:00Z",
        include_paid: true,
        attempt_no: 1,
        queued_at: "2026-11-03T05:00:00Z",
        started_at: "2026-11-03T05:00:01Z",
        finished_at: "2026-11-03T05:02:30Z",
      },
      age_s: 7200,
      partial_causes: ["fetch_failed"],
    },
    active_run_id: null,
    next_slots: ["2026-11-03T13:00:00Z", "2026-11-03T19:00:00Z", "2026-11-04T01:00:00Z"],
    quota: {
      jsearch: { used: 112, limit: 200, reset_at: "2026-11-16T09:00:00Z", state: "validated" },
      serpapi: { used: 3, limit: 250, reset_at: "2026-11-23T00:00:00Z", state: "validated" },
      adzuna: { used: 41, limit: 2250, reset_at: "2026-12-01T00:00:00Z", state: "validated" },
    },
    llm: { counted: "38.200000", cap: "100.00", pace: "ok" },
    coverage_gaps_open: 2,
    pending_targets: 212,
    alerts: { critical_unacked: 0, total_unacked: 1 },
    rescore: null,
    paid_search: { paused: false, until: null, reason: null },
    run_now: { jsearch_units: 6, serpapi_fallback_units: 0 },
    scope: { pending_version_id: null, progress: null, applied: true },
    banners: [],
    ...overrides,
  };
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

export function problem(status: number, code: string, extra: Record<string, unknown> = {}): Response {
  return new Response(
    JSON.stringify({ type: "about:blank", title: `${code} (server text)`, status, code, request_id: "r-1", ...extra }),
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

/** A request the stub received: its method, path, headers and string body. */
export interface FakeRequest {
  method: string;
  path: string;
  headers: Headers;
  body: string | null;
}

/** A route a test answers itself, by `METHOD path`; it is asked before the stub's own routes. */
export type FakeRoute = (request: FakeRequest) => Response;

export interface FakeApi {
  /** The bodies of the login requests, in order. */
  logins: Record<string, unknown>[];
  /** Every request, as `METHOD path`. */
  calls: string[];
  /** Every request, with its headers and body. */
  requests: FakeRequest[];
  signedIn: boolean;
  uiLanguage: "en" | "de";
  /** The display time zone the settings read answers. */
  timezone: string;
  /** The session read answers 503 (the api restarting, the proxy's 502 alike). */
  sessionDown: boolean;
  /** What `GET /api/system/status` answers. */
  status: StatusOut;
  /** The test's own answers, by `METHOD path` (`"POST /api/runs"`). */
  routes: Map<string, FakeRoute>;
}

/** The requests to `METHOD path`, in order. */
export function requestsTo(api: FakeApi, call: string): FakeRequest[] {
  return api.requests.filter((r) => `${r.method} ${r.path}` === call);
}

/** Installs the stub; `loginAnswers` answer the logins in order (a 204 opens the session). */
export function fakeApi({ signedIn = false, loginAnswers = [] as (() => Response)[] } = {}): FakeApi {
  const api: FakeApi = {
    logins: [],
    calls: [],
    requests: [],
    signedIn,
    uiLanguage: "en",
    timezone: "Europe/Berlin",
    sessionDown: false,
    status: statusOut(),
    routes: new Map(),
  };
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
    const method = (init?.method ?? "GET").toUpperCase();
    const request: FakeRequest = {
      method,
      path,
      headers: new Headers(init?.headers),
      body: typeof init?.body === "string" ? init.body : null,
    };
    api.calls.push(`${method} ${path}`);
    api.requests.push(request);
    if (method === "POST" && path === "/api/auth/login") {
      api.logins.push(JSON.parse(request.body ?? "{}") as Record<string, unknown>);
      const answer = loginAnswers.shift()?.() ?? problem(500, "internal");
      if (answer.status === 204) {
        api.signedIn = true;
      }
      return Promise.resolve(answer);
    }
    if (!api.signedIn) {
      return Promise.resolve(problem(401, "unauthenticated"));
    }
    const route = api.routes.get(`${method} ${path}`);
    if (route !== undefined) {
      return Promise.resolve(route(request));
    }
    if (method === "GET" && path === "/api/auth/session") {
      return Promise.resolve(api.sessionDown ? problem(503, "internal") : json(SESSION));
    }
    if (method === "GET" && path === "/api/settings") {
      return Promise.resolve(json(settingsRead(api.uiLanguage, api.timezone)));
    }
    if (method === "GET" && path === "/api/system/status") {
      return Promise.resolve(json(api.status));
    }
    if (method === "POST" && path === "/api/auth/logout") {
      api.signedIn = false;
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    return Promise.resolve(problem(404, "not_found"));
  });
  return api;
}
