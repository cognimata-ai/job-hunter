import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import { type FakeApi, fakeApi, json, problem, requestsTo, settingsRead, watchlistCompany } from "../test/api";
import { dryRunPayloads, formOf, renderSettings, saves } from "../test/settings";

type Settings = components["schemas"]["Settings"];
type SettingsOut = components["schemas"]["SettingsOut"];

afterEach(() => {
  vi.restoreAllMocks();
});

function section(): HTMLElement {
  const element = document.getElementById("watchlist");
  if (element === null) {
    throw new Error("no watchlist section");
  }
  return element;
}

function version(id: number, no: number, changes: Partial<Settings> = {}): SettingsOut {
  const base = settingsRead();
  return { ...base, version: { ...base.version, id, version_no: no }, payload: { ...base.payload, ...changes } };
}

function served(api: FakeApi, first: SettingsOut): { current: SettingsOut } {
  const state = { current: first };
  api.routes.set("GET /api/settings", () => json(state.current));
  return state;
}

/** `POST /api/watchlist/parse-url` by a few of the §5.4 patterns, as `watchlist_patterns.detect` reads them. */
function parsesUrls(api: FakeApi): void {
  api.routes.set("POST /api/watchlist/parse-url", (request) => {
    const { url } = JSON.parse(request.body ?? "{}") as { url: string };
    const lever = /^https:\/\/jobs\.(eu\.)?lever\.co\/([^/]+)/.exec(url);
    if (lever !== null) {
      return json({ ats: "lever", slug: lever[2], region: lever[1] === "eu." ? "eu" : "global" });
    }
    if (url.startsWith("https://apply.workable.com/")) {
      return json({ unsupported: "workable" });
    }
    return json({ unsupported: /^https?:\/\//.test(url) ? "unsupported_pattern" : "not_a_url" });
  });
}

function byUrl(): HTMLElement {
  return within(section()).getByRole("group", { name: en["settings_watchlist.by_url"] });
}

function byHand(): HTMLElement {
  return within(section()).getByRole("group", { name: en["settings_watchlist.by_hand"] });
}

async function list(): Promise<HTMLElement> {
  return within(section()).findByRole("table", { name: en["settings_watchlist.list_heading"] });
}

async function rowOf(name: string): Promise<HTMLElement> {
  const rows = within(await list()).getAllByRole("row");
  const row = rows.find((candidate) => within(candidate).queryByText(name) !== null);
  if (row === undefined) {
    throw new Error(`no row of ${name}`);
  }
  return row;
}

describe("WatchlistSection", () => {
  it("a Lever EU URL is detected with region and slug", async () => {
    const api = fakeApi({ signedIn: true });
    parsesUrls(api);
    renderSettings(["watchlist"]);
    const user = userEvent.setup();
    const url = await within(section()).findByRole("textbox", { name: en["settings_watchlist.url"] });
    await user.type(url, "https://jobs.eu.lever.co/acme-labs/");
    await within(byUrl()).findByRole("group", { name: en["settings_watchlist.detected"] });
    const value = (term: string) =>
      within(within(byUrl()).getByRole("group", { name: en["settings_watchlist.detected"] })).getByText(term).nextElementSibling
        ?.textContent;
    expect(value(en["settings_watchlist.ats"])).toBe(en["source.lever"]);
    expect(value(en["settings_watchlist.slug"])).toBe("acme-labs");
    expect(value(en["settings_watchlist.region"])).toBe(en["lever_region.eu"]);
    // the display name starts as the slug
    expect(within(byUrl()).getByRole<HTMLInputElement>("textbox", { name: en["settings_watchlist.name"] }).value).toBe("acme-labs");
    const [parse] = requestsTo(api, "POST /api/watchlist/parse-url");
    expect(JSON.parse(parse.body ?? "")).toEqual({ url: "https://jobs.eu.lever.co/acme-labs/" });
    // Detect reads at once
    await user.clear(url);
    await user.type(url, "https://jobs.lever.co/beta{Enter}");
    await waitFor(() => {
      expect(value(en["settings_watchlist.slug"])).toBe("beta");
    });
    expect(value(en["settings_watchlist.region"])).toBe(en["lever_region.global"]);
  });

  it("an unsupported URL shows its reason", async () => {
    const api = fakeApi({ signedIn: true });
    parsesUrls(api);
    renderSettings(["watchlist"]);
    const user = userEvent.setup();
    const url = await within(section()).findByRole("textbox", { name: en["settings_watchlist.url"] });
    await user.type(url, "https://apply.workable.com/acme/");
    await user.click(within(byUrl()).getByRole("button", { name: en["settings_watchlist.detect"] }));
    expect(await within(byUrl()).findByText(en["watchlist_unsupported.workable"])).toBeTruthy();
    expect(within(byUrl()).queryByRole("button", { name: en["settings_watchlist.add"] })).toBeNull();
    await user.clear(url);
    await user.type(url, "careers at acme{Enter}");
    expect(await within(byUrl()).findByText(en["watchlist_unsupported.not_a_url"])).toBeTruthy();
    expect(requestsTo(api, "POST /api/watchlist")).toHaveLength(0);
  });

  it("add posts and the list shows unvalidated", async () => {
    const api = fakeApi({ signedIn: true });
    parsesUrls(api);
    api.watchlist = [watchlistCompany()];
    const settings = served(api, version(41, 12, { watchlist: [{ watchlist_company_id: 4, enabled: true }] }));
    api.routes.set("POST /api/watchlist", (request) => {
      const body = JSON.parse(request.body ?? "{}") as { display_name: string };
      // the server's internal save added the entry
      settings.current = version(42, 13, {
        watchlist: [{ watchlist_company_id: 4, enabled: true }, { watchlist_company_id: 9, enabled: true }],
      });
      api.watchlist = [
        watchlistCompany(),
        watchlistCompany({
          id: 9,
          display_name: body.display_name,
          ats_type: "lever",
          board_slug: "acme-labs",
          lever_region: "eu",
          validation_status: "unvalidated",
          validated_at: null,
          instance: null,
          postings: { active: 0, stale: 0, unknown: 0, closed: 0 },
        }),
      ];
      return json({ id: 9, validation_status: "unvalidated" }, 201);
    });
    const { probe } = renderSettings(["watchlist"]);
    const user = userEvent.setup();
    await list();
    await user.type(within(section()).getByRole("textbox", { name: en["settings_watchlist.url"] }), "https://jobs.eu.lever.co/acme-labs{Enter}");
    const name = await within(byUrl()).findByRole("textbox", { name: en["settings_watchlist.name"] });
    await user.clear(name);
    await user.type(name, "Acme Labs");
    await user.click(within(byUrl()).getByRole("button", { name: en["settings_watchlist.add"] }));

    const [post] = requestsTo(api, "POST /api/watchlist");
    expect(JSON.parse(post.body ?? "")).toEqual({
      display_name: "Acme Labs",
      ats_type: "lever",
      board_slug: "acme-labs",
      lever_region: "eu",
      input_url: "https://jobs.eu.lever.co/acme-labs",
    });
    expect(post.headers.has("Idempotency-Key")).toBe(false);
    await waitFor(() => {
      expect(formOf(probe).base.version.id).toBe(42);
    });
    expect(formOf(probe).draft.watchlist).toEqual([
      { watchlist_company_id: 4, enabled: true },
      { watchlist_company_id: 9, enabled: true },
    ]);
    expect(formOf(probe).dirty.size).toBe(0);
    const row = await rowOf("Acme Labs");
    expect(row.textContent).toContain(en["watchlist_validation.unvalidated"]);
    expect(within(row).getByRole<HTMLInputElement>("checkbox").checked).toBe(true);
    expect(within(section()).getByText("“Acme Labs” is on the watchlist; its board is being validated")).toBeTruthy();
    // the URL is cleared, so the board is not offered again
    expect(within(section()).getByRole<HTMLInputElement>("textbox", { name: en["settings_watchlist.url"] }).value).toBe("");
    expect(within(byUrl()).queryByRole("group", { name: en["settings_watchlist.detected"] })).toBeNull();
    expect(saves(api)).toHaveLength(0);
  });

  it("an add of a board on the list says so, and a refused save shows its errors", async () => {
    const api = fakeApi({ signedIn: true });
    parsesUrls(api);
    let answer = () => problem(409, "conflict_state", { watchlist_company_id: 4 });
    api.routes.set("POST /api/watchlist", () => answer());
    renderSettings(["watchlist"]);
    const user = userEvent.setup();
    await user.type(await within(section()).findByRole("textbox", { name: en["settings_watchlist.url"] }), "https://jobs.lever.co/acme{Enter}");
    await user.click(await within(byUrl()).findByRole("button", { name: en["settings_watchlist.add"] }));
    expect(await within(byUrl()).findByText(en["settings_watchlist.exists"])).toBeTruthy();
    answer = () =>
      problem(422, "validation_failed", {
        errors: [{ field: "payload.watchlist", code: "limit", message: "List should have at most 100 items" }],
      });
    await user.click(within(byUrl()).getByRole("button", { name: en["settings_watchlist.add"] }));
    const alert = await within(byUrl()).findByRole("alert");
    expect(alert.textContent).toContain(en["problem.validation_failed"]);
    expect(within(byUrl()).getByRole("listitem").textContent).toBe(en["issue.limit"]);
    expect(within(byUrl()).queryByText(en["settings_watchlist.exists"])).toBeNull();
  });

  it("a Lever add needs the region", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/watchlist", () => json({ id: 9, validation_status: "unvalidated" }, 201));
    renderSettings(["watchlist"]);
    const user = userEvent.setup();
    const ats = await within(section()).findByRole("combobox", { name: en["settings_watchlist.ats"] });
    expect(within(byHand()).queryByRole("combobox", { name: en["settings_watchlist.region"] })).toBeNull();
    await user.selectOptions(ats, "lever");
    await user.type(within(byHand()).getByRole("textbox", { name: en["settings_watchlist.slug"] }), "acme");
    expect(within(byHand()).getByRole<HTMLInputElement>("textbox", { name: en["settings_watchlist.name"] }).value).toBe("acme");
    const add = within(byHand()).getByRole<HTMLButtonElement>("button", { name: en["settings_watchlist.add"] });
    expect(add.disabled).toBe(true);
    const required = within(byHand()).getByText(en["settings_watchlist.region_required"]);
    expect(add.getAttribute("aria-describedby")).toContain(required.id);
    await user.selectOptions(within(byHand()).getByRole("combobox", { name: en["settings_watchlist.region"] }), "eu");
    expect(add.disabled).toBe(false);
    await user.click(add);
    expect(JSON.parse(requestsTo(api, "POST /api/watchlist")[0]?.body ?? "")).toEqual({
      display_name: "acme",
      ats_type: "lever",
      board_slug: "acme",
      lever_region: "eu",
    });
    // no other ATS has a region
    await user.selectOptions(ats, "greenhouse");
    await user.type(within(byHand()).getByRole("textbox", { name: en["settings_watchlist.slug"] }), "beta");
    expect(within(byHand()).queryByRole("combobox", { name: en["settings_watchlist.region"] })).toBeNull();
    await user.click(within(byHand()).getByRole("button", { name: en["settings_watchlist.add"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/watchlist")).toHaveLength(2);
    });
    expect(JSON.parse(requestsTo(api, "POST /api/watchlist")[1]?.body ?? "")).toEqual({
      display_name: "beta",
      ats_type: "greenhouse",
      board_slug: "beta",
    });
  });

  it("lists each company with its validation, snapshot, health and postings, its enable in the draft", async () => {
    const api = fakeApi({ signedIn: true });
    api.watchlist = [
      watchlistCompany(),
      watchlistCompany({
        id: 6,
        display_name: "Beta",
        ats_type: "lever",
        board_slug: "beta",
        lever_region: "global",
        validation_status: "not_found",
        enabled: false,
        instance: { health: "breaker_open", last_error_code: "not_found", last_success_at: null, last_complete_snapshot_at: null },
        postings: { active: 0, stale: 1, unknown: 0, closed: 0 },
      }),
    ];
    served(api, version(41, 12, { watchlist: [{ watchlist_company_id: 4, enabled: true }] }));
    const { probe } = renderSettings(["watchlist"]);
    const user = userEvent.setup();
    const acme = await rowOf("Acme");
    expect(acme.textContent).toContain(en["watchlist_validation.ok"]);
    expect(acme.textContent).toContain("3 Nov, 06:00");
    expect(acme.textContent).toContain(en["instance_health.ok"]);
    expect(acme.textContent).toContain("12 active · 3 not re-confirmed · 0 unknown · 5 closed");
    const beta = await rowOf("Beta");
    expect(beta.textContent).toContain(`${en["source.lever"]} (${en["lever_region.global"]}) · beta`);
    expect(beta.textContent).toContain(en["watchlist_validation.not_found"]);
    expect(beta.textContent).toContain(en["settings_watchlist.never"]);
    expect(beta.textContent).toContain(en["instance_health.breaker_open"]);
    expect(beta.textContent).toContain(en["settings_queries.not_in_settings"]);
    const box = within(acme).getByRole<HTMLInputElement>("checkbox", { name: "Enable “Acme”" });
    await user.click(box);
    expect(formOf(probe).draft.watchlist).toEqual([{ watchlist_company_id: 4, enabled: false }]);
    await user.click(within(beta).getByRole("checkbox"));
    expect(formOf(probe).draft.watchlist).toEqual([
      { watchlist_company_id: 4, enabled: false },
      { watchlist_company_id: 6, enabled: true },
    ]);
    expect([...formOf(probe).dirty].sort()).toEqual(["watchlist[id=4].enabled", "watchlist[id=6]"]);
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.watchlist).toEqual([
        { watchlist_company_id: 4, enabled: false },
        { watchlist_company_id: 6, enabled: true },
      ]);
    });
    expect(api.calls.filter((call) => call.startsWith("DELETE") || call === "POST /api/watchlist")).toEqual([]);
  });

  it("remove asks first", async () => {
    const api = fakeApi({ signedIn: true });
    api.watchlist = [watchlistCompany()];
    served(api, version(41, 12, { watchlist: [{ watchlist_company_id: 4, enabled: true }] }));
    renderSettings(["watchlist"]);
    const user = userEvent.setup();
    await user.click(within(await rowOf("Acme")).getByRole("button", { name: "Remove “Acme”" }));
    const dialog = screen.getByRole("alertdialog", { name: "Remove Acme from the watchlist?" });
    expect(within(dialog).getByText(en["settings_watchlist.remove_body"])).toBeTruthy();
    await user.click(within(dialog).getByRole("button", { name: en["action.cancel"] }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(requestsTo(api, "DELETE /api/watchlist/4")).toHaveLength(0);
  });

  it("remove rebases", async () => {
    const api = fakeApi({ signedIn: true });
    api.watchlist = [watchlistCompany(), watchlistCompany({ id: 6, display_name: "Beta", board_slug: "beta" })];
    const settings = served(
      api,
      version(41, 12, { watchlist: [{ watchlist_company_id: 4, enabled: true }, { watchlist_company_id: 6, enabled: true }] }),
    );
    api.routes.set("DELETE /api/watchlist/4", () => {
      settings.current = version(42, 13, { watchlist: [{ watchlist_company_id: 6, enabled: true }] });
      api.watchlist = [watchlistCompany({ id: 6, display_name: "Beta", board_slug: "beta" })];
      return new Response(null, { status: 204 });
    });
    const { probe } = renderSettings(["watchlist"]);
    const user = userEvent.setup();
    // an unsaved change elsewhere stays
    await user.click(within(await rowOf("Beta")).getByRole("checkbox"));
    await user.click(within(await rowOf("Acme")).getByRole("button", { name: "Remove “Acme”" }));
    const dialog = screen.getByRole("alertdialog", { name: "Remove Acme from the watchlist?" });
    await user.click(within(dialog).getByRole("button", { name: en["settings_watchlist.remove"] }));
    const [removal] = requestsTo(api, "DELETE /api/watchlist/4");
    expect(removal.headers.has("Idempotency-Key")).toBe(false);
    await waitFor(() => {
      expect(formOf(probe).base.version.id).toBe(42);
    });
    expect(formOf(probe).draft.watchlist).toEqual([{ watchlist_company_id: 6, enabled: false }]);
    expect([...formOf(probe).dirty]).toEqual(["watchlist[id=6].enabled"]);
    await waitFor(() => {
      expect(within(section()).queryByText("Acme")).toBeNull();
    });
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(saves(api)).toHaveLength(0);
  });
});
