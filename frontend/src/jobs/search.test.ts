import { createMemoryHistory } from "@tanstack/react-router";
import { render, waitFor, within } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App, type AppParts, createApp } from "../app/App";
import en from "../i18n/en.json";
import { fakeApi, requestsTo } from "../test/api";
import { DEFAULT_SEARCH, type JobsSearch, parseJobsSearch, resetSearch, toApiParams } from "./search";

afterEach(() => {
  vi.restoreAllMocks();
});

async function at(path: string): Promise<AppParts> {
  const app = createApp({ history: createMemoryHistory({ initialEntries: [path] }) });
  const { container } = render(createElement(App, { app }));
  await within(container).findByRole("heading", { level: 1, name: en["jobs.title"] });
  return app;
}

/** The Jobs route's search as the page reads it (after `validateSearch`). */
function routeSearch(app: AppParts): JobsSearch {
  const match = app.router.state.matches.find((m) => m.routeId === "/");
  if (match === undefined) {
    throw new Error("the Jobs route is not matched");
  }
  return match.search;
}

/** Every filter off its default, the location's label, the drawer and Label mode. */
const EVERY_FILTER: JobsSearch = {
  sort: "posted",
  q: "Entwickler",
  posted: "custom",
  posted_from: "2026-10-01",
  posted_to: "2026-10-31",
  score_min: 40,
  score_max: 90,
  coverage_min: 60,
  confidence: "medium",
  loc_id: "geonames:2950159",
  loc_lat: 52.52437,
  loc_lon: 13.41053,
  radius_km: 30,
  loc_label: "Berlin",
  include_remote: false,
  work_mode: ["remote", "hybrid"],
  company: ["acme", "beta-ag"],
  source: ["greenhouse", "lever"],
  lang: ["de"],
  employment: ["full_time", "unknown"],
  seniority: ["junior", "senior"],
  status: ["unseen", "saved", "hidden"],
  availability: ["active", "stale"],
  contested: "exclude",
  partial: "only",
  scoring_state: ["scored", "not_a_job_ad"],
  job: 42,
  label: 1,
};

describe("the Jobs search", () => {
  it("defaults are omitted from the URL and the API params", async () => {
    expect(parseJobsSearch({})).toEqual(DEFAULT_SEARCH);
    expect(DEFAULT_SEARCH).toEqual({
      sort: "score_mid",
      posted: "30d",
      score_min: 0,
      score_max: 100,
      coverage_min: 0,
      confidence: "any",
      include_remote: true,
      work_mode: ["remote", "hybrid", "onsite", "unknown"],
      company: [],
      source: [],
      lang: ["de", "en", "other", "unknown"],
      employment: [],
      seniority: [],
      status: ["unseen", "seen", "saved", "applied"],
      availability: ["active", "unknown"],
      contested: "any",
      partial: "any",
      scoring_state: ["scored", "unscored", "filtered", "failed", "expired"],
    });
    expect(toApiParams(DEFAULT_SEARCH).toString()).toBe("");
    expect(toApiParams({ ...DEFAULT_SEARCH, sort: "posted", job: 7, label: 1 }).toString()).toBe("sort=posted");
    fakeApi({ signedIn: true });
    const app = await at("/?sort=score_mid&posted=30d&work_mode=hybrid&work_mode=remote&work_mode=unknown&work_mode=onsite");
    await waitFor(() => {
      expect(app.router.state.location.searchStr).toBe(""); // every value given is a default: none stays
    });
    await app.router.navigate({ to: "/", search: { ...DEFAULT_SEARCH, sort: "posted" } });
    expect(app.router.state.location.searchStr).toBe("?sort=posted");
    await app.router.navigate({ to: "/", search: DEFAULT_SEARCH });
    expect(app.router.state.location.searchStr).toBe("");
  });

  it("invalid values fall back", () => {
    const cases: [Record<string, unknown>, Partial<JobsSearch>][] = [
      [{ sort: "salary" }, { sort: "score_mid" }],
      [{ q: "   " }, {}],
      [{ q: "x".repeat(201) }, {}],
      [{ q: "  python  " }, { q: "python" }],
      [{ q: "a\u0000b" }, {}],
      [{ posted: "1y" }, { posted: "30d" }],
      [{ posted: "custom" }, { posted: "30d" }], // no dates
      [{ posted: "custom", posted_from: "2026-10-31", posted_to: "2026-10-01" }, { posted: "30d" }], // out of order
      [{ posted: "custom", posted_from: "2026-02-30", posted_to: "2026-03-01" }, { posted: "30d" }], // no such day
      [{ posted: "7d", posted_from: "2026-10-01", posted_to: "2026-10-31" }, { posted: "7d" }], // dates without custom
      [{ score_min: "70", score_max: "40" }, { score_min: 0, score_max: 100 }],
      [{ score_min: "-1", score_max: "101" }, { score_min: 0, score_max: 100 }],
      [{ score_min: "4.5" }, { score_min: 0 }],
      [{ coverage_min: "abc" }, { coverage_min: 0 }],
      [{ confidence: "low" }, { confidence: "any" }],
      [{ include_remote: "no" }, { include_remote: true }],
      [{ work_mode: ["remote", "office"] }, { work_mode: ["remote"] }], // an invalid member is dropped
      [{ work_mode: "office" }, { work_mode: DEFAULT_SEARCH.work_mode }], // a set left empty is the default
      [{ status: ["hidden", "hidden"] }, { status: ["hidden"] }],
      [{ employment: ["full_time", "gig"] }, { employment: ["full_time"] }],
      [{ seniority: "wizard" }, { seniority: [] }],
      [{ company: ["", "acme"] }, { company: ["acme"] }],
      [{ contested: "maybe", partial: "" }, { contested: "any", partial: "any" }],
      [{ job: "0" }, {}],
      [{ job: "abc" }, {}],
      [{ job: "5" }, { job: 5 }],
      [{ label: "2" }, {}],
      [{ label: "1" }, { label: 1 }],
      [{ sort: ["posted", "first_seen"] }, { sort: "score_mid" }], // a repeated single value
    ];
    for (const [raw, expected] of cases) {
      expect(parseJobsSearch(raw), JSON.stringify(raw)).toEqual({ ...DEFAULT_SEARCH, ...expected });
    }
    const place = { loc_id: "geonames:2950159", loc_lat: "52.52437", loc_lon: "13.41053", radius_km: "30", loc_label: "Berlin" };
    expect(parseJobsSearch(place)).toEqual({
      ...DEFAULT_SEARCH,
      loc_id: "geonames:2950159",
      loc_lat: 52.52437,
      loc_lon: 13.41053,
      radius_km: 30,
      loc_label: "Berlin",
    });
    // a location needs all four, each valid, or it is dropped whole (its label with it)
    for (const broken of [
      { ...place, loc_id: "Berlin" },
      { ...place, loc_lat: "91" },
      { ...place, loc_lon: "Infinity" },
      { ...place, loc_lon: "" },
      { ...place, radius_km: "0" },
      { ...place, radius_km: "201" },
      { loc_id: place.loc_id, loc_lat: place.loc_lat, loc_lon: place.loc_lon, loc_label: "Berlin" },
    ]) {
      expect(parseJobsSearch(broken), JSON.stringify(broken)).toEqual(DEFAULT_SEARCH);
    }
    expect(() => parseJobsSearch({ score_min: { $gt: 1 }, work_mode: [1, null], q: 5 })).not.toThrow();
  });

  it("include_remote belongs to the location", () => {
    const place = { loc_id: "geonames:2950159", loc_lat: "52.52437", loc_lon: "13.41053", radius_km: "30" };
    expect(parseJobsSearch({ include_remote: "false" })).toEqual(DEFAULT_SEARCH); // no place: its default
    expect(toApiParams(parseJobsSearch({ include_remote: "false" })).has("include_remote")).toBe(false);
    expect(toApiParams({ ...DEFAULT_SEARCH, include_remote: false }).has("include_remote")).toBe(false);
    const near = parseJobsSearch({ ...place, include_remote: "false" });
    expect(near.include_remote).toBe(false);
    expect(toApiParams(near).get("include_remote")).toBe("false");
  });

  it("resetSearch restores the defaults, keeps Label mode and closes the drawer", () => {
    expect(resetSearch(EVERY_FILTER)).toEqual({ ...DEFAULT_SEARCH, label: 1 });
    expect(resetSearch({ ...EVERY_FILTER, label: undefined })).toEqual(DEFAULT_SEARCH);
    expect(resetSearch(DEFAULT_SEARCH)).toEqual(DEFAULT_SEARCH);
  });

  it("round trip of every filter", async () => {
    fakeApi({ signedIn: true });
    const app = await at("/");
    await app.router.navigate({ to: "/", search: EVERY_FILTER });
    const searchStr = app.router.state.location.searchStr;
    expect(routeSearch(app)).toEqual(EVERY_FILTER);
    expect(searchStr).toContain("work_mode=remote&work_mode=hybrid"); // a set as repeated keys
    expect(searchStr).toContain("q=Entwickler");
    expect(searchStr).toContain("label=1");
    expect(searchStr).not.toContain("%22"); // no JSON quoting
    // the same URL, read afresh, is the same search
    const again = await at(`/${searchStr}`);
    expect(routeSearch(again)).toEqual(EVERY_FILTER);
    // the API params: every filter, sorted, without the label, the drawer and Label mode
    const params = toApiParams(EVERY_FILTER);
    expect([...new Set(params.keys())]).toEqual([
      "availability",
      "company",
      "confidence",
      "contested",
      "coverage_min",
      "employment",
      "include_remote",
      "lang",
      "loc_id",
      "loc_lat",
      "loc_lon",
      "partial",
      "posted",
      "posted_from",
      "posted_to",
      "q",
      "radius_km",
      "score_max",
      "score_min",
      "scoring_state",
      "seniority",
      "sort",
      "source",
      "status",
      "work_mode",
    ]);
    expect(params.getAll("work_mode")).toEqual(["hybrid", "remote"]);
    expect(params.getAll("status")).toEqual(["hidden", "saved", "unseen"]);
    expect(params.get("include_remote")).toBe("false");
    expect(params.get("loc_lat")).toBe("52.52437");
    expect(params.get("posted_from")).toBe("2026-10-01");
    // the order the sets were given in does not change the params (the list's query key)
    const shuffled: JobsSearch = { ...EVERY_FILTER, work_mode: ["hybrid", "remote"], status: ["hidden", "unseen", "saved"] };
    expect(toApiParams(shuffled).toString()).toBe(params.toString());
  });

  it("a place picker id such as geonames:2950159 round-trips through the URL into loc_id unchanged", async () => {
    const api = fakeApi({ signedIn: true });
    const app = await at("/?loc_id=geonames:2950159&loc_lat=52.52437&loc_lon=13.41053&radius_km=30&loc_label=Berlin");
    expect(routeSearch(app).loc_id).toBe("geonames:2950159");
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs").at(-1)?.search.get("loc_id")).toBe("geonames:2950159");
    });
    expect(app.router.state.location.search).toMatchObject({ loc_id: "geonames:2950159", radius_km: "30" });
    await app.router.navigate({ to: "/", search: { ...routeSearch(app), loc_id: "geonames:2867714", loc_label: "München" } });
    const searchStr = app.router.state.location.searchStr;
    expect(new URLSearchParams(searchStr).get("loc_id")).toBe("geonames:2867714");
    expect(routeSearch(await at(`/${searchStr}`))).toMatchObject({ loc_id: "geonames:2867714", loc_label: "München" });
  });
});
