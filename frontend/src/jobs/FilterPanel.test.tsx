import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppParts } from "../app/App";
import codes from "../i18n/codes.json";
import de from "../i18n/de.json";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import { type FakeApi, fakeApi, jobCard, jobsPage, json, place, requestsTo } from "../test/api";
import { NOW, renderApp } from "../test/app";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
});

afterEach(async () => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

const FACETS = {
  companies: [
    { company_key: "acme", display_name: "Acme GmbH", count: 12 },
    { company_key: "beta-ag", display_name: "Beta AG", count: 3 },
  ],
  sources: [
    { source_id: "greenhouse", count: 8 },
    { source_id: "lever", count: 5 },
  ],
};

async function panel(api: FakeApi, path = "/"): Promise<{ app: AppParts; aside: HTMLElement }> {
  api.jobs = jobsPage([jobCard()]);
  api.facets = FACETS;
  api.places = [
    place(),
    place({ gazetteer_id: "geonames:2911298", label: "Hamburg", postal_codes: ["20095", "20097"], population: 1845229 }),
    place({ gazetteer_id: "geonames:2950096", label: "Bernau", postal_codes: [], population: 40129 }),
  ];
  const app = renderApp(path);
  await screen.findByRole("article", { name: "Senior Backend Engineer" });
  return { app, aside: screen.getByRole("complementary", { name: en["filters.title"] }) };
}

function group(aside: HTMLElement, name: string): HTMLElement {
  return within(aside).getByRole("group", { name });
}

function box(scope: HTMLElement, name: string): HTMLInputElement {
  return within(scope).getByRole<HTMLInputElement>("checkbox", { name });
}

/** The URL's search as the router parsed it (repeated keys as arrays, every value a string). */
function url(app: AppParts): Record<string, unknown> {
  return app.router.state.location.search;
}

describe("FilterPanel", () => {
  it("each filter writes its search param", async () => {
    const api = fakeApi({ signedIn: true });
    const { app, aside } = await panel(api);
    const user = userEvent.setup();
    const history = () => app.router.history.length;
    let entries = history();

    // Search: debounced, replacing the history entry
    await user.type(within(aside).getByRole("searchbox", { name: en["filters.search"] }), "Entwickler");
    expect(url(app)).toEqual({});
    await waitFor(() => {
      expect(url(app)).toEqual({ q: "Entwickler" });
    });
    expect(history()).toBe(entries);

    // Posted within: a radio, pushing an entry; custom with its two dates
    const posted = group(aside, en["filters.posted"]);
    await user.click(within(posted).getByRole("radio", { name: en["filters.posted.7d"] }));
    await waitFor(() => {
      expect(url(app)).toMatchObject({ posted: "7d" });
    });
    expect(history()).toBe(entries + 1);
    entries = history();
    await user.click(within(posted).getByRole("radio", { name: en["filters.posted.custom"] }));
    await waitFor(() => {
      expect(url(app)).toMatchObject({ posted: "custom", posted_from: "2026-10-04", posted_to: "2026-11-03" });
    });
    expect(history()).toBe(entries + 1);
    entries = history();
    const from = within(posted).getByLabelText<HTMLInputElement>(en["filters.posted_from"]);
    await user.clear(from);
    await user.type(from, "2026-10-20");
    await waitFor(() => {
      expect(url(app)).toMatchObject({ posted_from: "2026-10-20", posted_to: "2026-11-03" });
    });

    // Score range and Min coverage: sliders, written when a change is committed, replacing the entry (as the
    // custom dates did)
    const score = group(aside, en["filters.score"]);
    within(score).getByRole("slider", { name: en["filters.score_min"] }).focus();
    await user.keyboard("{PageUp}");
    await waitFor(() => {
      expect(url(app)).toMatchObject({ score_min: "10" });
    });
    expect(score.textContent).toContain("10–100");
    within(score).getByRole("slider", { name: en["filters.score_max"] }).focus();
    await user.keyboard("{ArrowLeft}");
    await waitFor(() => {
      expect(url(app)).toMatchObject({ score_min: "10", score_max: "99" });
    });
    const coverage = group(aside, en["filters.coverage"]);
    within(coverage).getByRole("slider", { name: en["filters.coverage"] }).focus();
    await user.keyboard("{PageUp}");
    await waitFor(() => {
      expect(url(app)).toMatchObject({ coverage_min: "10" });
    });
    expect(coverage.textContent).toContain("10%");
    expect(history()).toBe(entries);

    // Confidence: a native select
    await user.selectOptions(within(aside).getByRole("combobox", { name: en["filters.confidence"] }), en["filters.confidence.medium"]);
    await waitFor(() => {
      expect(url(app)).toMatchObject({ confidence: "medium" });
    });
    expect(history()).toBe(entries + 1);

    // Location: a picked place, its radius (30 km first) and include remote
    const location = group(aside, en["filters.location"]);
    await user.type(within(location).getByRole("combobox", { name: en["filters.place"] }), "Ber");
    await user.click(await within(location).findByRole("option", { name: /^Berlin/ }));
    await waitFor(() => {
      expect(url(app)).toMatchObject({ loc_id: "geonames:2950159", loc_lat: "52.52437", loc_lon: "13.41053", radius_km: "30", loc_label: "Berlin" });
    });
    const radius = within(location).getByRole<HTMLInputElement>("spinbutton", { name: en["filters.radius"] });
    expect(radius.value).toBe("30");
    await user.clear(radius);
    await user.type(radius, "50");
    await waitFor(() => {
      expect(url(app)).toMatchObject({ radius_km: "50" });
    });
    await user.click(box(location, en["filters.include_remote"]));
    await waitFor(() => {
      expect(url(app)).toMatchObject({ include_remote: "false" });
    });

    // the checkbox sets
    await user.click(box(group(aside, en["filters.work_mode"]), en["filters.work_mode.onsite"]));
    await user.click(box(group(aside, en["filters.work_mode"]), en["filters.work_mode.unknown"]));
    await user.click(box(group(aside, en["filters.lang"]), en["filters.lang.other"]));
    await user.click(box(group(aside, en["filters.employment"]), en["employment.full_time"]));
    await user.click(box(group(aside, en["filters.seniority"]), en["seniority.senior"]));
    await user.click(box(group(aside, en["filters.status"]), en["filters.status.hidden"]));
    await user.click(box(group(aside, en["filters.availability"]), en["filters.availability.closed"]));
    await user.click(box(group(aside, en["filters.scoring_state"]), en["filters.scoring_state.not_a_job_ad"]));
    await user.click(box(group(aside, en["filters.source"]), "Greenhouse (8)"));
    await waitFor(() => {
      expect(url(app)).toMatchObject({
        work_mode: ["remote", "hybrid"],
        lang: ["de", "en", "unknown"],
        employment: "full_time",
        seniority: "senior",
        status: ["unseen", "seen", "saved", "applied", "hidden"],
        availability: ["active", "unknown", "closed"],
        scoring_state: ["scored", "unscored", "filtered", "failed", "expired", "not_a_job_ad"],
        source: "greenhouse",
      });
    });

    // Company: the picker over the facets, its picks as removable chips
    const company = group(aside, en["filters.company"]);
    await user.type(within(company).getByRole("combobox", { name: en["filters.company_search"] }), "ac");
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs/facets").some((r) => r.search.get("company_q") === "ac")).toBe(true);
    });
    await user.click(await within(company).findByRole("option", { name: "Acme GmbH (12)" }));
    await waitFor(() => {
      expect(url(app)).toMatchObject({ company: "acme" });
    });
    expect(within(company).getByRole("button", { name: "Remove Acme GmbH" })).toBeTruthy();

    // Contested and Partial: radios
    await user.click(within(group(aside, en["filters.contested"])).getByRole("radio", { name: en["filters.tri.only"] }));
    await user.click(within(group(aside, en["filters.partial"])).getByRole("radio", { name: en["filters.tri.exclude"] }));
    await waitFor(() => {
      expect(url(app)).toMatchObject({ contested: "only", partial: "exclude" });
    });

    // Label mode: the param alone (Task 26 builds the mode)
    const labelMode = within(aside).getByRole("button", { name: en["filters.label_mode"] });
    expect(labelMode.getAttribute("aria-pressed")).toBe("false");
    await user.click(labelMode);
    await waitFor(() => {
      expect(url(app)).toMatchObject({ label: "1" });
    });
    expect(labelMode.getAttribute("aria-pressed")).toBe("true");

    // the list is read under every filter, and the facets under the same ones
    await waitFor(() => {
      const last = requestsTo(api, "GET /api/jobs").at(-1)?.search;
      expect(last?.get("partial")).toBe("exclude");
      expect(last?.get("q")).toBe("Entwickler");
      expect(last?.get("radius_km")).toBe("50");
      expect(last?.getAll("company")).toEqual(["acme"]);
      expect(last?.has("label")).toBe(false);
      expect(last?.has("loc_label")).toBe(false);
    });
    const facets = requestsTo(api, "GET /api/jobs/facets").at(-1)?.search;
    expect(facets?.get("partial")).toBe("exclude");

    // Back undoes the last discrete change
    app.router.history.back();
    await waitFor(() => {
      expect(url(app)).not.toHaveProperty("label");
    });
    expect(url(app)).toMatchObject({ partial: "exclude" });
  });

  it("the place picker shows postal codes and population", async () => {
    const api = fakeApi({ signedIn: true });
    const { aside } = await panel(api);
    const user = userEvent.setup();
    const location = group(aside, en["filters.location"]);
    await user.type(within(location).getByRole("combobox", { name: en["filters.place"] }), "Ber");
    const options = await within(location).findAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual([
      "Berlin · 10115, 10117, 10119 +2 · 3,426,354", // three postal codes, then the rest counted
      "Hamburg · 20095, 20097 · 1,845,229",
      "Bernau · 40,129", // no postal code
    ]);
    const search = requestsTo(api, "GET /api/geo/places").at(-1)?.search;
    expect(search?.get("q")).toBe("Ber");
    expect(search?.get("limit")).toBe("10");
    expect(requestsTo(api, "GET /api/geo/places").every((r) => r.search.get("q") !== "B")).toBe(true); // debounced
  });

  it("the place picker shows the population in German", async () => {
    const api = fakeApi({ signedIn: true });
    api.uiLanguage = "de";
    api.jobs = jobsPage([jobCard()]);
    api.places = [place()];
    renderApp("/");
    const aside = await screen.findByRole("complementary", { name: de["filters.title"] });
    const location = within(aside).getByRole("group", { name: de["filters.location"] });
    await userEvent.setup().type(within(location).getByRole("combobox", { name: de["filters.place"] }), "Ber");
    expect((await within(location).findByRole("option")).textContent).toBe("Berlin · 10115, 10117, 10119 +2 · 3.426.354");
  });

  it("the place picker is a combobox operable by keyboard", async () => {
    const api = fakeApi({ signedIn: true });
    const { app, aside } = await panel(api);
    const user = userEvent.setup();
    const input = within(aside).getByRole<HTMLInputElement>("combobox", { name: en["filters.place"] });
    const listbox = document.getElementById(input.getAttribute("aria-controls") ?? "");
    expect(listbox?.getAttribute("role")).toBe("listbox");
    expect(input.getAttribute("aria-expanded")).toBe("false");
    expect(input.getAttribute("aria-autocomplete")).toBe("list");

    // Down on an empty field lists the most populous places
    input.focus();
    await user.keyboard("{ArrowDown}");
    expect(input.getAttribute("aria-expanded")).toBe("true");
    const options = await within(listbox as HTMLElement).findAllByRole("option");
    expect(requestsTo(api, "GET /api/geo/places").at(-1)?.search.get("q")).toBe("");
    expect(input.getAttribute("aria-activedescendant")).toBe(options[0].id);
    expect(options[0].getAttribute("aria-selected")).toBe("true");
    await user.keyboard("{ArrowDown}");
    expect(input.getAttribute("aria-activedescendant")).toBe(options[1].id);
    expect(options[0].getAttribute("aria-selected")).toBe("false");
    await user.keyboard("{ArrowUp}");
    expect(input.getAttribute("aria-activedescendant")).toBe(options[0].id);

    // Esc closes without a pick
    await user.keyboard("{Escape}");
    expect(input.getAttribute("aria-expanded")).toBe("false");
    expect(input.hasAttribute("aria-activedescendant")).toBe(false);
    expect(url(app)).toEqual({});

    // Enter picks the active option
    await user.type(input, "Ham");
    await within(listbox as HTMLElement).findByRole("option", { name: /^Hamburg/ });
    await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    await waitFor(() => {
      expect(url(app)).toMatchObject({ loc_id: "geonames:2911298", loc_label: "Hamburg", radius_km: "30" });
    });
    expect(input.getAttribute("aria-expanded")).toBe("false");
    expect(input.value).toBe("Hamburg");
    expect(document.activeElement).toBe(input);

    // Tab leaves without a pick
    await user.clear(input);
    await user.type(input, "Ber");
    await within(listbox as HTMLElement).findByRole("option", { name: /^Berlin/ });
    await user.keyboard("{ArrowDown}");
    await user.tab();
    expect(input.getAttribute("aria-expanded")).toBe("false");
    expect(url(app)).toMatchObject({ loc_id: "geonames:2911298" });
    expect(input.value).toBe("Hamburg"); // the picked place's name again

    // clearing the place removes all four location params and the label
    await user.click(within(aside).getByRole("button", { name: en["filters.location_clear"] }));
    await waitFor(() => {
      expect(url(app)).toEqual({});
    });
    expect(input.value).toBe("");
  });

  it("the last ticked work mode cannot be unticked", async () => {
    const api = fakeApi({ signedIn: true });
    const { app, aside } = await panel(api, "/?work_mode=remote&work_mode=hybrid");
    const user = userEvent.setup();
    const modes = group(aside, en["filters.work_mode"]);
    expect(box(modes, en["filters.work_mode.remote"]).disabled).toBe(false);
    await user.click(box(modes, en["filters.work_mode.hybrid"]));
    await waitFor(() => {
      expect(url(app)).toEqual({ work_mode: "remote" });
    });
    const last = box(modes, en["filters.work_mode.remote"]);
    expect(last.checked).toBe(true);
    expect(last.disabled).toBe(true);
    const why = document.getElementById(last.getAttribute("aria-describedby") ?? "");
    expect(why?.textContent).toBe(en["filters.last_one"]);
    expect(modes.contains(why)).toBe(true);
    await user.click(last);
    expect(url(app)).toEqual({ work_mode: "remote" });
    expect(box(modes, en["filters.work_mode.onsite"]).disabled).toBe(false);
    for (const name of ["filters.lang", "filters.status", "filters.availability", "filters.scoring_state"] as const) {
      expect(within(group(aside, en[name])).queryByText(en["filters.last_one"]), name).toBeNull();
    }
    // the same guard on the other closed sets
    await user.click(box(group(aside, en["filters.availability"]), en["filters.availability.unknown"]));
    await waitFor(() => {
      expect(box(group(aside, en["filters.availability"]), en["filters.availability.active"]).disabled).toBe(true);
    });
  });

  it("reset restores the defaults", async () => {
    const api = fakeApi({ signedIn: true });
    // Label mode (Task 26) shows the labelling queue in the list's place: here the same job, to label
    api.routes.set("GET /api/labelling/queue", () =>
      json({
        items: [{ job_id: 5, title: "Senior Backend Engineer", company: "Acme GmbH", locations: ["Berlin"], ad_language: "en", excerpt: "" }],
        progress: { labelled: 0, target: 50 },
      }),
    );
    const { app, aside } = await panel(
      api,
      "/?q=python&sort=posted&work_mode=remote&score_min=40&loc_id=geonames:2950159&loc_lat=52.5&loc_lon=13.4&radius_km=30&loc_label=Berlin&job=5&label=1",
    );
    const user = userEvent.setup();
    const search = within(aside).getByRole<HTMLInputElement>("searchbox", { name: en["filters.search"] });
    expect(search.value).toBe("python");
    await user.click(within(aside).getByRole("button", { name: en["filters.reset"] }));
    await waitFor(() => {
      expect(app.router.state.location.searchStr).toBe("?label=1"); // Label mode stays, the drawer closes
    });
    expect(search.value).toBe("");
    expect(within(aside).getByRole<HTMLInputElement>("combobox", { name: en["filters.place"] }).value).toBe("");
    expect(box(group(aside, en["filters.work_mode"]), en["filters.work_mode.onsite"]).checked).toBe(true);
    expect(within(group(aside, en["filters.score"])).getByRole("slider", { name: en["filters.score_min"] }).getAttribute("aria-valuenow")).toBe("0");
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs").at(-1)?.search.toString()).toBe("limit=50");
    });
  });

  it("lists the seniority levels in the ladder's order", async () => {
    const api = fakeApi({ signedIn: true });
    const { aside } = await panel(api);
    const boxes = within(group(aside, en["filters.seniority"])).getAllByRole("checkbox");
    expect(codes.order.seniority).toEqual(["intern", "working_student", "apprentice", "junior", "mid", "senior", "lead", "principal", "head"]);
    expect(boxes.map((input) => input.closest("label")?.textContent)).toEqual(
      codes.order.seniority.map((code) => en[`seniority.${code}` as keyof typeof en]),
    );
  });

  it("clearing the place also clears include remote, which belongs to it", async () => {
    const api = fakeApi({ signedIn: true });
    const { app, aside } = await panel(
      api,
      "/?loc_id=geonames:2950159&loc_lat=52.52437&loc_lon=13.41053&radius_km=30&loc_label=Berlin&include_remote=false",
    );
    const location = group(aside, en["filters.location"]);
    expect(box(location, en["filters.include_remote"]).checked).toBe(false);
    await userEvent.setup().click(within(location).getByRole("button", { name: en["filters.location_clear"] }));
    await waitFor(() => {
      expect(url(app)).toEqual({});
    });
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs").at(-1)?.search.toString()).toBe("limit=50");
    });
  });

  it("company counts read in the UI language's numbers", async () => {
    const api = fakeApi({ signedIn: true });
    api.uiLanguage = "de";
    api.jobs = jobsPage([jobCard()]);
    api.facets = { companies: [{ company_key: "acme", display_name: "Acme GmbH", count: 1284 }], sources: [] };
    renderApp("/");
    const aside = await screen.findByRole("complementary", { name: de["filters.title"] });
    const company = within(aside).getByRole("group", { name: de["filters.company"] });
    await userEvent.setup().type(within(company).getByRole("combobox", { name: de["filters.company_search"] }), "ac");
    expect((await within(company).findByRole("option")).textContent).toBe("Acme GmbH (1.284)");
  });

  it("collapses behind its Filters button on a narrow screen", async () => {
    const api = fakeApi({ signedIn: true });
    const { aside } = await panel(api);
    const toggle = within(aside).getByRole("button", { name: en["filters.title"] });
    expect(toggle.getAttribute("aria-expanded")).toBe("false"); // closed by default (shown by CSS above 768 px)
    const body = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
    expect(aside.contains(body)).toBe(true);
    expect(body?.contains(within(aside).getByRole("searchbox", { name: en["filters.search"] }))).toBe(true);
    await userEvent.setup().click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });
});
