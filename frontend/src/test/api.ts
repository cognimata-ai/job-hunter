// A stub of the api behind `fetch`, for tests that render the app: a session that a login opens and a test
// can end, the session, settings, header-status, Jobs-list, facets, places, profile and spend reads, the
// logout, the login's answers in the order a test gives them, and any other route a test answers itself
// (`routes`). A route is matched by its path alone; the query string is the request's `search`.
import { vi } from "vitest";
import type { components } from "../api/schema";

type SessionOut = components["schemas"]["SessionOut"];
type StatusOut = components["schemas"]["StatusOut"];
type JobCardOut = components["schemas"]["JobCardOut"];
type JobsPageOut = components["schemas"]["JobsPageOut"];
type SpendOut = components["schemas"]["SpendOut"];
type FacetsOut = components["schemas"]["FacetsOut"];
type PlaceOut = components["schemas"]["PlaceOut"];
type ProfileListOut = components["schemas"]["ProfileListOut"];
type CountsOut = components["schemas"]["CountsOut"];

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

/** A card of the Jobs list: job 5, a pending (unscored) ad at Acme in Berlin, posted two days before `NOW`
 * (07:00 UTC on 3 Nov 2026), first seen the day before and last seen two hours before, not yet acted on. */
export function jobCard(overrides: Partial<JobCardOut> = {}): JobCardOut {
  return {
    job_id: 5,
    kind: "job",
    title: "Senior Backend Engineer",
    company: "Acme GmbH",
    company_key: "acme",
    locations: ["Berlin"],
    location_kind: "city",
    work_modes: ["hybrid"],
    work_mode_known: true,
    posted_at: "2026-11-01T07:00:00Z",
    posted_precision: "exact",
    first_seen_at: "2026-11-02T07:00:00Z",
    last_seen_at: "2026-11-03T05:00:00Z",
    source_ids: ["greenhouse"],
    availability: "active",
    completeness: "full",
    ad_language: "en",
    user: null,
    score: null,
    target: { id: 50, state: "pending", state_reason: null, refusal_category: null, similarity: 0.62, override: "none" },
    bucket: "unscored",
    badges: [],
    knockouts: [],
    filter_reasons: [],
    filter_keyword: null,
    previous: null,
    possible_duplicate_of: null,
    group: null,
    group_badge: null,
    links: { open_ad: "https://boards.example.com/acme/jobs/5", adzuna: null },
    ...overrides,
  };
}

type JobDetailOut = components["schemas"]["JobDetailOut"];
type DetailScoreOut = components["schemas"]["DetailScoreOut"];
type HistoryItemOut = components["schemas"]["HistoryItemOut"];
type UserActionsOut = components["schemas"]["UserActionsOut"];

/** The description of `jobDetail`: two stored paragraphs, one per line (`normalize/html.py`). "Python" is
 * [20, 26), "FastAPI" [35, 42) and "PostgreSQL" [47, 57) in code points. */
export const DESCRIPTION = "Backend services in Python.\nWe use FastAPI and PostgreSQL.";

/** The score of `jobDetail`: 82 [74, 90], accepted by the critic, the six LLM criteria and the two
 * deterministic ones with evidence on `skills` and the profile side of `role_fit`. */
export function detailScore(overrides: Partial<DetailScoreOut> = {}): DetailScoreOut {
  const llm = (criterion: string, weight: number | null, level: number | null, basis: string) =>
    ({ criterion, weight, level, basis, rationale: "", evidence: [] });
  return {
    mid: "82.40",
    low: "74.00",
    high: "89.50",
    coverage: "0.850",
    confidence: "high",
    result_status: "accepted",
    result_id: 31,
    det_result_id: 61,
    critic_outcome: "accepted",
    injection_suspected: false,
    versions: {
      profile_version_no: 4,
      rubric_version: "v1",
      prompt_schema_hash: "3f2a1b2c9d8e7f60a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718",
      generator: { model: "gen-model", effort: "medium" },
      critic: { model: "critic-model", effort: "high" },
    },
    summary_en: "A strong match for backend work.",
    summary_de: "Eine starke Passung für Backend-Arbeit.",
    criteria: [
      {
        ...llm("role_fit", 20, 4, "observed"),
        evidence: [
          { subject: "role_fit", source_kind: "description", field_name: null, quote: "Backend services", span_start: 0, span_end: 16 },
          { subject: "role_fit", source_kind: "profile", field_name: "roles", quote: "Backend Engineer", span_start: null, span_end: null },
        ],
      },
      {
        ...llm("skills", 25, 3, "observed"),
        evidence: [
          { subject: "skills", source_kind: "description", field_name: null, quote: "FastAPI", span_start: 35, span_end: 42 },
        ],
      },
      llm("experience_seniority", 15, 3, "observed"),
      llm("domain", 10, null, "unknown"),
      llm("languages", 10, 4, "observed"),
      llm("education_certifications", 5, 4, "unconstrained"),
    ],
    fact_evidence: [],
    det_criteria: [
      { criterion: "location_work_mode", weight: 10, level: 4, basis: "observed", inputs: { compat: { hybrid: 4 }, hybrid_band: 4, onsite_band: 4 } },
      { criterion: "employment_salary", weight: 5, level: 4, basis: "observed", inputs: { et: 4, sal: 4 } },
    ],
    knockouts: [],
    caps_applied: [],
    industry_match: "neutral",
    shortened_fields: [],
    corrected_fields: [],
    scored_via_job_id: null,
    ...overrides,
  };
}

/** A result of a job's history: published the day before `NOW`, with its current instance. */
export function historyItem(overrides: Partial<HistoryItemOut> = {}): HistoryItemOut {
  return {
    result_id: 31,
    published_at: "2026-11-02T09:00:00Z",
    profile_version_no: 4,
    rubric_version: "v1",
    llm_tuple_id: 2,
    status: "accepted",
    instances: [
      {
        det_result_id: 61,
        scope_version_id: 3,
        is_current: true,
        mid: "82.40",
        low: "74.00",
        high: "89.50",
        coverage: "0.850",
        confidence: "high",
        computed_at: "2026-11-02T09:00:00Z",
      },
    ],
    ...overrides,
  };
}

/** The user's actions on a job as the api answers them: none set, unless `row` sets some. */
export function userActions(jobId: number, row: Partial<UserActionsOut> = {}): UserActionsOut {
  return {
    job_id: jobId,
    seen_at: null,
    saved_at: null,
    applied_at: null,
    hidden_at: null,
    note: null,
    updated_at: "2026-11-03T06:00:00Z",
    ...row,
  };
}

/** The detail of `jobCard()`'s job 5 (`GET /api/jobs/5`), scored (`detailScore`), never opened (`user: null`): in
 * Berlin, hybrid by Lever's structured field, full-time at €70,000–85,000 a year (stated), posted on 1 Nov (day
 * precision), with one Lever posting and one page of history. */
export function jobDetail(overrides: Partial<JobDetailOut> = {}): JobDetailOut {
  const provenance = (rule: string) => ({
    source_id: "lever",
    source_instance_id: 7,
    posting_id: 50,
    observation_id: 900,
    observed_at: "2026-11-03T05:00:00Z",
    rule,
  });
  return {
    job: {
      job_id: 5,
      title: "Senior Backend Engineer",
      company: "Acme GmbH",
      company_key: "acme",
      locations: ["Berlin"],
      location_kind: "city",
      work_modes: ["hybrid"],
      work_mode_known: true,
      work_mode_source_id: "lever",
      posted_at: "2026-11-01T07:00:00Z",
      posted_precision: "day",
      first_seen_at: "2026-11-02T07:00:00Z",
      last_seen_at: "2026-11-03T05:00:00Z",
      source_ids: ["lever"],
      availability: "active",
      completeness: "full",
      ad_language: "en",
      employment_types: ["full_time"],
      salary: { min: "70000", max: "85000", currency: "EUR", period: "year", kind: "stated" },
      field_provenance: { title: provenance("source_rank"), work_mode_possible: provenance("structured_intersection") },
    },
    description: { text: DESCRIPTION, lang: "en", completeness: "full", from_job_id: 5 },
    postings: [
      {
        id: 50,
        source_id: "lever",
        native_id: "abc-123",
        first_seen_at: "2026-11-02T07:00:00Z",
        last_seen_at: "2026-11-03T05:00:00Z",
        availability: "active",
        link: "https://jobs.lever.co/acme/abc-123",
      },
    ],
    links: { open_ad: "https://jobs.lever.co/acme/abc-123/apply", adzuna: null },
    score: detailScore(),
    contested: null,
    target: { id: 50, state: "published_accepted", state_reason: null, refusal_category: null, similarity: 0.7, override: "none" },
    bucket: "scored",
    group: null,
    merge_events: [],
    user: null,
    feedback: [],
    history: { items: [historyItem()], next_cursor: null },
    ...overrides,
  };
}

type GroupOut = components["schemas"]["GroupOut"];
type GroupMemberOut = components["schemas"]["GroupMemberOut"];

/** A member of a duplicate group as the detail's `group` lists it: job 5, an Adzuna ad at Acme in Berlin with a
 * partial description, deferred to the group's representative. */
export function groupMember(overrides: Partial<GroupMemberOut> = {}): GroupMemberOut {
  return {
    job_id: 5,
    title: "Senior Backend Engineer",
    company: "Acme GmbH",
    source_ids: ["adzuna"],
    locations: ["Berlin"],
    completeness: "partial",
    availability: "active",
    target_state: "deferred_duplicate",
    score_individually: false,
    is_representative: false,
    ...overrides,
  };
}

/** Duplicate group 3 as `GET /api/jobs/{id}` answers its `group`: the representative job 9 (JSearch and
 * Greenhouse, scored), elected `initial`, and its deferred members 5 (Adzuna) and 6 (Arbeitnow, not
 * re-confirmed), by job id as the api lists them. */
export function groupOut(overrides: Partial<GroupOut> = {}): GroupOut {
  return {
    id: 3,
    status: "active",
    representative_job_id: 9,
    unscorable_reason: null,
    election_reason: "initial",
    members: [
      groupMember(),
      groupMember({
        job_id: 6,
        title: "Backend Engineer (m/w/d)",
        source_ids: ["arbeitnow"],
        locations: ["Berlin", "Potsdam"],
        completeness: "full",
        availability: "stale",
      }),
      groupMember({
        job_id: 9,
        title: "Backend Engineer",
        source_ids: ["jsearch", "greenhouse"],
        completeness: "full",
        target_state: "published_accepted",
        is_representative: true,
      }),
    ],
    ...overrides,
  };
}

/** The list's card of `groupOut()`: its representative job 9 (kind `group`), scored 82, with the group's three
 * members and its two deferred ones, 5 and 6. */
export function groupCard(overrides: Partial<JobCardOut> = {}): JobCardOut {
  return jobCard({
    job_id: 9,
    kind: "group",
    title: "Backend Engineer",
    source_ids: ["jsearch", "greenhouse"],
    score: { mid: "82.40", low: "74.00", high: "89.50", coverage: "0.850", confidence: "high", result_status: "accepted" },
    target: { id: 90, state: "published_accepted", state_reason: null, refusal_category: null, similarity: 0.7, override: "none" },
    bucket: "scored",
    group: {
      group_id: 3,
      size: 3,
      representative: { job_id: 9, title: "Backend Engineer", source_ids: ["jsearch", "greenhouse"] },
      members: [
        { job_id: 5, title: "Senior Backend Engineer", source_ids: ["adzuna"], availability: "active", completeness: "partial", target_state: "deferred_duplicate" },
        { job_id: 6, title: "Backend Engineer (m/w/d)", source_ids: ["arbeitnow"], availability: "stale", completeness: "full", target_state: "deferred_duplicate" },
      ],
    },
    links: { open_ad: "https://boards.example.com/acme/jobs/9", adzuna: null },
    ...overrides,
  });
}

/** The detail of `groupCard()`'s representative, job 9, already seen, in `group` (`groupOut()` by default). */
export function representativeDetail(group: GroupOut | null = groupOut()): JobDetailOut {
  const detail = jobDetail();
  return jobDetail({
    job: { ...detail.job, job_id: 9, title: "Backend Engineer", source_ids: ["jsearch", "greenhouse"] },
    description: { ...detail.description, from_job_id: 9 },
    group,
    user: userActions(9, { seen_at: "2026-11-02T08:00:00Z" }),
  });
}

/** A first page of the Jobs list holding `items`, with no further page. */
export function jobsPage(items: JobCardOut[] = []): JobsPageOut {
  return {
    items,
    next_cursor: null,
    ranking_generation: 1,
    ranking_changed: false,
    counts: { total_estimate: items.length, unscored: items.filter((c) => c.score === null).length, capped: false },
  };
}

/** A paged Jobs list (`pagedJobs`): its pages, the ranking generation now, and the route that answers it. */
export interface PagedList {
  /** The cards of each page, in order; a test may change them between reads. */
  pages: JobCardOut[][];
  /** The ranking generation now. A cursor carries the generation of its first page, and a later page whose
   * cursor carries an older one says `ranking_changed` (spec §14.3). */
  generation: number;
  /** The first page's counts. */
  counts: CountsOut;
  route: FakeRoute;
}

/**
 * `GET /api/jobs` over `pages`, as the api pages it: a first page (no cursor) at the current generation with
 * the counts, its `next_cursor` `c1.g<generation>`; a cursor `c<i>.g<g>` answers page `i` without counts, with
 * `ranking_changed` when the generation has moved past `g`, and the next cursor keeps `g`. Any other cursor, or
 * one past the last page, is 400 `invalid_cursor`.
 */
export function pagedJobs(pages: JobCardOut[][], counts?: CountsOut): PagedList {
  const list: PagedList = {
    pages,
    generation: 1,
    counts: counts ?? {
      total_estimate: pages.flat().length,
      unscored: pages.flat().filter((card) => card.score === null).length,
      capped: false,
    },
    route: (request) => {
      const cursor = request.search.get("cursor");
      const at = cursor === null ? [0, list.generation] : /^c(\d+)\.g(\d+)$/.exec(cursor)?.slice(1).map(Number);
      const [index, gen] = at ?? [-1, 0];
      if (index < 0 || index >= list.pages.length) {
        return problem(400, "invalid_cursor");
      }
      const page: JobsPageOut = {
        items: list.pages[index],
        next_cursor: index + 1 < list.pages.length ? `c${String(index + 1)}.g${String(gen)}` : null,
        ranking_generation: list.generation,
        ranking_changed: list.generation > gen,
        counts: index === 0 ? list.counts : null,
      };
      return json(page);
    },
  };
  return list;
}

/** The answer of `GET /api/profile`: an active profile version (v3), or none before a CV is confirmed. */
export function profileList(active = true): ProfileListOut {
  const version = { id: 3, version_no: 3, confirmed_at: "2026-10-30T09:00:00Z", purged: false, profile: null };
  return {
    active: active ? version : null,
    versions: active ? [version] : [],
    open_draft: null,
    latest_proposal: null,
    estimates: { extract_profile_usd: "0.040000", propose_queries_usd: "0.020000" },
  };
}

/** A gazetteer place as `GET /api/geo/places` answers it. */
export function place(overrides: Partial<PlaceOut> = {}): PlaceOut {
  return {
    gazetteer_id: "geonames:2950159",
    label: "Berlin",
    postal_codes: ["10115", "10117", "10119", "10178", "10179"],
    lat: 52.52437,
    lon: 13.41053,
    population: 3426354,
    ...overrides,
  };
}

/** The month's spend: $38.20 of $100.00 counted, and an estimated $0.052646 per scored ad (`c_hat`). */
export function spendOut(overrides: Partial<SpendOut> = {}): SpendOut {
  return {
    month: "2026-11-01",
    counted: "38.200000",
    billed: "38.200000",
    ambiguous: "0.000000",
    reserved: "0.000000",
    cap: "100.00",
    headroom: "61.800000",
    bucket: { usd: "1.200000", capacity: "1.551667", rate_per_h: "0.129306" },
    c_hat: "0.052646",
    c_hat_source: "history",
    critic_rate_7d: null,
    projected_critic_rate: "0.450",
    bulk_operation: null,
    quarantined_batch_usd: "0.000000",
    pause_reason: null,
    by_model_effort_role: [],
    eval_by_model_role: [],
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

/** A request the stub received: its method, path (without the query string), query, headers and string body. */
export interface FakeRequest {
  method: string;
  path: string;
  search: URLSearchParams;
  headers: Headers;
  body: string | null;
}

/** A route a test answers itself, by `METHOD path`; it is asked before the stub's own routes. A promise holds
 * the answer until the test settles it. */
export type FakeRoute = (request: FakeRequest) => Response | Promise<Response>;

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
  /** What `GET /api/jobs` answers (an empty page by default). */
  jobs: JobsPageOut;
  /** What `GET /api/system/spend` answers. */
  spend: SpendOut;
  /** What `GET /api/jobs/facets` answers (none by default). */
  facets: FacetsOut;
  /** What `GET /api/geo/places` answers (none by default). */
  places: PlaceOut[];
  /** Whether `GET /api/profile` has an active profile version (it has by default). */
  profileActive: boolean;
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
    jobs: jobsPage(),
    spend: spendOut(),
    facets: { companies: [], sources: [] },
    places: [],
    profileActive: true,
    routes: new Map(),
  };
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://api.test");
    const path = url.pathname;
    const method = (init?.method ?? "GET").toUpperCase();
    const request: FakeRequest = {
      method,
      path,
      search: url.searchParams,
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
    if (method === "GET" && path === "/api/jobs") {
      return Promise.resolve(json(api.jobs));
    }
    if (method === "GET" && path === "/api/system/spend") {
      return Promise.resolve(json(api.spend));
    }
    if (method === "GET" && path === "/api/jobs/facets") {
      return Promise.resolve(json(api.facets));
    }
    if (method === "GET" && path === "/api/geo/places") {
      return Promise.resolve(json(api.places));
    }
    if (method === "GET" && path === "/api/profile") {
      return Promise.resolve(json(profileList(api.profileActive)));
    }
    if (method === "POST" && path === "/api/auth/logout") {
      api.signedIn = false;
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    return Promise.resolve(problem(404, "not_found"));
  });
  return api;
}
