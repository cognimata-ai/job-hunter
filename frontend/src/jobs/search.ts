// The Jobs page's search (spec §15 "URL-synced", §15.3; plan M4 Task 23, Ruling b): the sort and every §15.3
// filter under the names of the list's query parameters (`jobs/query.py` `JobsQuery`, `GET /api/jobs`), the
// place's label, the drawer's job (`job`) and Label mode (`label`).
//
// - `DEFAULT_SEARCH` is `JobsQuery`'s defaults, the ★ of §15.3.
// - `parseJobsSearch` reads the URL's search (every value a string, a repeated key an array: the router's
//   `parseSearch`) or an already typed one, and never throws: each field that is unknown or invalid falls back
//   to its default on its own. A set keeps its valid members in one order (a closed set in its declared order,
//   an open one sorted), so two equal searches are equal, and the router leaves the defaults out of the URL.
// - `toApiParams` is the list's query: no default, no label, drawer or Label mode, the keys and each set's values
//   sorted, so equal searches give equal params (the list's query key).
import type { operations } from "../api/schema";
import codes from "../i18n/codes.json";

type ListQuery = NonNullable<operations["list_jobs_api_jobs_get"]["parameters"]["query"]>;
type Member<T> = NonNullable<T> extends readonly (infer M)[] ? M : never;

export type JobSort = NonNullable<ListQuery["sort"]>;
export type PostedWindow = NonNullable<ListQuery["posted"]>;
export type Confidence = NonNullable<ListQuery["confidence"]>;
export type Tri = NonNullable<ListQuery["contested"]>;
export type WorkMode = Member<ListQuery["work_mode"]>;
export type AdLanguage = Member<ListQuery["lang"]>;
export type UserStatus = Member<ListQuery["status"]>;
export type Availability = Member<ListQuery["availability"]>;
export type ScoringState = Member<ListQuery["scoring_state"]>;

/** The members of a closed set of the API, in this order: a member missing here, or one the API does not
 * have, fails the typecheck. */
function members<T extends string>(all: Record<T, true>): readonly T[] {
  return Object.keys(all) as T[];
}

export const SORTS = members<JobSort>({ score_mid: true, score_low: true, posted: true, first_seen: true });
export const POSTED_WINDOWS = members<PostedWindow>({
  "24h": true,
  "3d": true,
  "7d": true,
  "14d": true,
  "30d": true,
  any: true,
  custom: true,
});
export const CONFIDENCES = members<Confidence>({ any: true, medium: true, high: true });
export const TRI = members<Tri>({ any: true, only: true, exclude: true });
export const WORK_MODES = members<WorkMode>({ remote: true, hybrid: true, onsite: true, unknown: true });
export const AD_LANGUAGES = members<AdLanguage>({ de: true, en: true, other: true, unknown: true });
export const USER_STATUSES = members<UserStatus>({ unseen: true, seen: true, saved: true, applied: true, hidden: true });
export const AVAILABILITIES = members<Availability>({ active: true, unknown: true, stale: true, closed: true });
export const SCORING_STATES = members<ScoringState>({
  scored: true,
  unscored: true,
  filtered: true,
  failed: true,
  expired: true,
  not_a_job_ad: true,
});
/** The employment types and `unknown`, and the seniority levels: the API types them as strings, the code
 * catalogue lists them (derived from `EMPLOYMENT_VALUES` and `SENIORITY_LADDER`). */
export const EMPLOYMENT_TYPES: readonly string[] = codes.employment;
export const SENIORITY_LEVELS: readonly string[] = codes.seniority;

/** The largest search text, in characters (`JobsQuery.q`). */
const MAX_Q = 200;
const MAX_LABEL = 200;
const GAZETTEER_ID = /^geonames:[0-9]{1,12}$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const DECIMAL = /^[+-]?(\d+(\.\d*)?|\.\d+)$/;
const UNSIGNED = /^\d+$/;
// a NUL, or a lone surrogate: neither reaches PostgreSQL (`JobsQuery` refuses them)
const UNSTORABLE = /\0|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

export interface JobsSearch {
  sort: JobSort;
  /** The title or keyword search, trimmed, 1 to 200 characters. */
  q?: string;
  posted: PostedWindow;
  /** With `posted: "custom"` only, both or neither: ISO dates, `posted_from` not after `posted_to`. */
  posted_from?: string;
  posted_to?: string;
  score_min: number;
  score_max: number;
  coverage_min: number;
  confidence: Confidence;
  /** The place: all four of `loc_id` (its `gazetteer_id`, as `GET /api/geo/places` gives it), `loc_lat`,
   * `loc_lon` and `radius_km` (1 to 200), or none; `loc_label` (its name, for the picker) only with them. */
  loc_id?: string;
  loc_lat?: number;
  loc_lon?: number;
  radius_km?: number;
  loc_label?: string;
  include_remote: boolean;
  work_mode: readonly WorkMode[];
  /** Company keys; empty: any company. */
  company: readonly string[];
  /** Source ids; empty: any source. */
  source: readonly string[];
  lang: readonly AdLanguage[];
  /** Employment types (or `unknown`); empty: any. */
  employment: readonly string[];
  /** Seniority levels; empty: any. */
  seniority: readonly string[];
  status: readonly UserStatus[];
  availability: readonly Availability[];
  contested: Tri;
  partial: Tri;
  scoring_state: readonly ScoringState[];
  /** The job whose drawer is open (Task 24). */
  job?: number;
  /** Label mode (Task 26). */
  label?: 1;
}

/** `JobsQuery`'s defaults (the ★ of §15.3); the optional fields are absent. */
export const DEFAULT_SEARCH: JobsSearch = {
  sort: "score_mid",
  posted: "30d",
  score_min: 0,
  score_max: 100,
  coverage_min: 0,
  confidence: "any",
  include_remote: true,
  work_mode: WORK_MODES,
  company: [],
  source: [],
  lang: AD_LANGUAGES,
  employment: [],
  seniority: [],
  status: USER_STATUSES.filter((status) => status !== "hidden"),
  availability: ["active", "unknown"],
  contested: "any",
  partial: "any",
  scoring_state: SCORING_STATES.filter((state) => state !== "not_a_job_ad"),
};

/** A single value as text: a string, or a finite number or a boolean as typed; anything else (a repeated key's
 * array, an object) is no value. */
function scalar(value: unknown): string | undefined {
  if (typeof value === "string") {
    return value;
  }
  if ((typeof value === "number" && Number.isFinite(value)) || typeof value === "boolean") {
    return String(value);
  }
  return undefined;
}

/** A set's values as given: a repeated key's array, or a key given once. */
function listed(value: unknown): string[] {
  if (typeof value === "string") {
    return [value];
  }
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  const text = scalar(value);
  return allowed.find((member) => member === text) ?? fallback;
}

/** An integer from `min` to `max` written in digits, else `undefined`. */
function integer(value: unknown, min: number, max: number): number | undefined {
  const text = scalar(value);
  if (text === undefined || !UNSIGNED.test(text)) {
    return undefined;
  }
  const n = Number(text);
  return n >= min && n <= max ? n : undefined;
}

/** A decimal number from `min` to `max`, else `undefined`. */
function decimal(value: unknown, min: number, max: number): number | undefined {
  const text = scalar(value);
  if (text === undefined || !DECIMAL.test(text)) {
    return undefined;
  }
  const n = Number(text);
  return Number.isFinite(n) && n >= min && n <= max ? n : undefined;
}

/** A valid calendar date `YYYY-MM-DD`, else `undefined`. */
function isoDate(value: unknown): string | undefined {
  const text = scalar(value);
  if (text === undefined || !ISO_DATE.test(text)) {
    return undefined;
  }
  const date = new Date(`${text}T00:00:00Z`);
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === text ? text : undefined;
}

/** Trimmed text of 1 to `max` characters that PostgreSQL can hold, else `undefined`. */
function text(value: unknown, max: number): string | undefined {
  const trimmed = scalar(value)?.trim();
  // `max` counts characters as the api does (code points), not UTF-16 units
  if (trimmed === undefined || trimmed === "" || Array.from(trimmed).length > max || UNSTORABLE.test(trimmed)) {
    return undefined;
  }
  return trimmed;
}

/** The members of a closed set given in `value`, in `order`; none given or none valid: `fallback`. */
function closedSet<T extends string>(value: unknown, order: readonly T[], fallback: readonly T[]): readonly T[] {
  const given = new Set(listed(value));
  const kept = order.filter((member) => given.has(member));
  return kept.length === 0 ? fallback : kept;
}

/** The keys given in `value` (a company key, a source id): non-empty, storable, without repeats, sorted. */
function openSet(value: unknown): string[] {
  const kept = listed(value).filter((key) => key !== "" && !UNSTORABLE.test(key));
  return [...new Set(kept)].sort();
}

type Place = Pick<JobsSearch, "loc_id" | "loc_lat" | "loc_lon" | "radius_km" | "loc_label">;

/** All four location fields, each valid, and the label; else none of them. */
function location(raw: Record<string, unknown>): Place {
  const id = scalar(raw.loc_id);
  const lat = decimal(raw.loc_lat, -90, 90);
  const lon = decimal(raw.loc_lon, -180, 180);
  const radius = integer(raw.radius_km, 1, 200);
  if (id === undefined || !GAZETTEER_ID.test(id) || lat === undefined || lon === undefined || radius === undefined) {
    return {};
  }
  const label = text(raw.loc_label, MAX_LABEL);
  return { loc_id: id, loc_lat: lat, loc_lon: lon, radius_km: radius, ...(label === undefined ? {} : { loc_label: label }) };
}

function postedWindow(raw: Record<string, unknown>): Pick<JobsSearch, "posted" | "posted_from" | "posted_to"> {
  const posted = oneOf(raw.posted, POSTED_WINDOWS, DEFAULT_SEARCH.posted);
  if (posted !== "custom") {
    return { posted };
  }
  const from = isoDate(raw.posted_from);
  const to = isoDate(raw.posted_to);
  return from !== undefined && to !== undefined && from <= to ? { posted, posted_from: from, posted_to: to } : { posted: DEFAULT_SEARCH.posted };
}

function scoreRange(raw: Record<string, unknown>): Pick<JobsSearch, "score_min" | "score_max"> {
  const low = integer(raw.score_min, 0, 100) ?? DEFAULT_SEARCH.score_min;
  const high = integer(raw.score_max, 0, 100) ?? DEFAULT_SEARCH.score_max;
  return low <= high ? { score_min: low, score_max: high } : { score_min: DEFAULT_SEARCH.score_min, score_max: DEFAULT_SEARCH.score_max };
}

/** The search of `raw` (module comment): never throws, each invalid field at its default. */
export function parseJobsSearch(raw: Record<string, unknown>): JobsSearch {
  const q = text(raw.q, MAX_Q);
  const employment = new Set(listed(raw.employment));
  const seniority = new Set(listed(raw.seniority));
  const job = integer(raw.job, 1, Number.MAX_SAFE_INTEGER);
  return {
    sort: oneOf(raw.sort, SORTS, DEFAULT_SEARCH.sort),
    ...(q === undefined ? {} : { q }),
    ...postedWindow(raw),
    ...scoreRange(raw),
    coverage_min: integer(raw.coverage_min, 0, 100) ?? DEFAULT_SEARCH.coverage_min,
    confidence: oneOf(raw.confidence, CONFIDENCES, DEFAULT_SEARCH.confidence),
    ...location(raw),
    include_remote: oneOf(raw.include_remote, ["true", "false"], "true") === "true",
    work_mode: closedSet(raw.work_mode, WORK_MODES, DEFAULT_SEARCH.work_mode),
    company: openSet(raw.company),
    source: openSet(raw.source),
    lang: closedSet(raw.lang, AD_LANGUAGES, DEFAULT_SEARCH.lang),
    employment: EMPLOYMENT_TYPES.filter((value) => employment.has(value)),
    seniority: SENIORITY_LEVELS.filter((value) => seniority.has(value)),
    status: closedSet(raw.status, USER_STATUSES, DEFAULT_SEARCH.status),
    availability: closedSet(raw.availability, AVAILABILITIES, DEFAULT_SEARCH.availability),
    contested: oneOf(raw.contested, TRI, DEFAULT_SEARCH.contested),
    partial: oneOf(raw.partial, TRI, DEFAULT_SEARCH.partial),
    scoring_state: closedSet(raw.scoring_state, SCORING_STATES, DEFAULT_SEARCH.scoring_state),
    ...(job === undefined ? {} : { job }),
    ...(scalar(raw.label) === "1" ? { label: 1 as const } : {}),
  };
}

const DEFAULTS: ReadonlyMap<string, unknown> = new Map(Object.entries(DEFAULT_SEARCH));
/** The fields the API does not read: the place's label, the drawer and Label mode. */
const NOT_API: ReadonlySet<string> = new Set(["loc_label", "job", "label"]);

function sameSet(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((value) => b.includes(value));
}

/** `s` as the list's query parameters (module comment): a set as one repeated key per value. */
export function toApiParams(s: JobsSearch): URLSearchParams {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(s) as [string, unknown][]) {
    const fallback = DEFAULTS.get(name);
    if (NOT_API.has(name) || value === undefined || value === fallback) {
      continue;
    }
    if (Array.isArray(value)) {
      const values = value as string[];
      if (!Array.isArray(fallback) || !sameSet(values, fallback as string[])) {
        for (const item of [...values].sort()) {
          params.append(name, item);
        }
      }
      continue;
    }
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      params.append(name, String(value));
    }
  }
  params.sort();
  return params;
}
