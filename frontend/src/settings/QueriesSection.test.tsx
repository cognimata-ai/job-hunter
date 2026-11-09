import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import { type FakeApi, cellStats, fakeApi, json, problem, queryRow, requestsTo, settingsRead } from "../test/api";
import { dryRunPayloads, formOf, renderSettings, saves } from "../test/settings";
import { QUERIES_KEY } from "./queriesApi";

type Settings = components["schemas"]["Settings"];
type SettingsOut = components["schemas"]["SettingsOut"];

afterEach(() => {
  vi.restoreAllMocks();
});

function section(): HTMLElement {
  const element = document.getElementById("queries");
  if (element === null) {
    throw new Error("no queries section");
  }
  return element;
}

/** Settings version `id` (number `no`) of the default document with `changes` written over it. */
function version(id: number, no: number, changes: Partial<Settings> = {}): SettingsOut {
  const base = settingsRead();
  return { ...base, version: { ...base.version, id, version_no: no }, payload: { ...base.payload, ...changes } };
}

/** The settings the stub answers, which a test changes as an internal save would. */
function served(api: FakeApi, first: SettingsOut): { current: SettingsOut } {
  const state = { current: first };
  api.routes.set("GET /api/settings", () => json(state.current));
  return state;
}

async function table(): Promise<HTMLElement> {
  return within(section()).findByRole("table", { name: en["settings_queries.heading"] });
}

/** The table's row of the query `text`. */
async function rowOf(text: string): Promise<HTMLElement> {
  const rows = within(await table()).getAllByRole("row");
  const row = rows.find((candidate) => within(candidate).queryByText(text) !== null);
  if (row === undefined) {
    throw new Error(`no row of ${text}`);
  }
  return row;
}

/** The cell of `row` in the column `header`. */
function cellOf(row: HTMLElement, header: string): HTMLElement {
  const headers = within(section()).getAllByRole("columnheader").map((th) => th.textContent);
  const index = headers.indexOf(header);
  if (index < 0) {
    throw new Error(`no column ${header}`);
  }
  return within(row).getAllByRole("cell")[index];
}

function checkbox(row: HTMLElement): HTMLInputElement {
  return within(row).getByRole<HTMLInputElement>("checkbox");
}

function writes(api: FakeApi): string[] {
  return api.calls.filter((call) => !call.startsWith("GET ") && call !== "POST /api/settings/dry-run");
}

describe("QueriesSection", () => {
  it("renders the table with too broad and gaps", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [
      queryRow({ id: 1, too_broad: true, open_gaps: 3 }),
      queryRow({
        id: 2,
        text: "Softwareentwickler Python",
        lang: "de",
        origin: "llm_proposal",
        exploration: true,
        proposal_id: 5,
        last_covered_at: null,
        open_gaps: 0,
        yield_ema: null,
        refresh_cadence_days: 10,
      }),
    ];
    served(api, version(41, 12, { queries: [{ query_id: 1, enabled: true }, { query_id: 2, enabled: false }] }));
    renderSettings(["queries"]);

    const first = await rowOf("Backend Engineer");
    expect(cellOf(first, en["settings_queries.col.lang"]).textContent).toBe(en["language_code.en"]);
    expect(cellOf(first, en["settings_queries.col.origin"]).textContent).toBe(en["query_origin.user"]);
    expect(cellOf(first, en["settings_queries.col.exploration"]).textContent).toBe(en["value.no"]);
    expect(cellOf(first, en["settings_queries.col.last_covered"]).textContent).toBe("3 Nov, 06:00");
    expect(cellOf(first, en["settings_queries.col.open_gaps"]).textContent).toBe("3");
    expect(cellOf(first, en["settings_queries.col.yield"]).textContent).toBe("1.25");
    expect(cellOf(first, en["settings_queries.col.cadence"]).textContent).toBe("3.5 days");
    expect(cellOf(first, en["settings_queries.col.hint"]).textContent).toBe(en["settings_queries.too_broad"]);
    expect(checkbox(first).checked).toBe(true);

    const second = await rowOf("Softwareentwickler Python");
    expect(cellOf(second, en["settings_queries.col.lang"]).textContent).toBe(en["language_code.de"]);
    expect(cellOf(second, en["settings_queries.col.origin"]).textContent).toBe(en["query_origin.llm_proposal"]);
    expect(cellOf(second, en["settings_queries.col.exploration"]).textContent).toBe(en["value.yes"]);
    expect(cellOf(second, en["settings_queries.col.last_covered"]).textContent).toBe(en["settings_queries.never"]);
    expect(cellOf(second, en["settings_queries.col.open_gaps"]).textContent).toBe("0");
    expect(cellOf(second, en["settings_queries.col.yield"]).textContent).toBe(en["value.none"]);
    expect(cellOf(second, en["settings_queries.col.cadence"]).textContent).toBe("10 days");
    expect(cellOf(second, en["settings_queries.col.hint"]).textContent).toBe("");
    expect(checkbox(second).checked).toBe(false);
    expect(within(second).queryByText(en["settings_entries.not_in_settings"])).toBeNull();
  });

  it("shows a row's cells from one read of the stats", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 1 }), queryRow({ id: 2, text: "Data Engineer" })];
    api.queryStats = [
      cellStats({ cell_id: 100, query_id: 1, too_broad: true, consecutive_incomplete: 3 }),
      cellStats({ cell_id: 101, query_id: 1, source_id: "serpapi", location_key: "remote-de", cadence_days: null, open_gaps: 0 }),
      cellStats({ cell_id: 102, query_id: 2, location_key: "hamburg" }),
    ];
    const base = settingsRead().payload;
    const berlin = { label: "Berlin", gazetteer_id: "geonames:2950159", lat: 52.52437, lon: 13.41053, radius_km: 30 };
    served(api, version(41, 12, { scope: { ...base.scope, locations: [berlin] }, queries: [{ query_id: 1, enabled: true }] }));
    renderSettings(["queries"]);
    const user = userEvent.setup();
    const first = await rowOf("Backend Engineer");
    expect(requestsTo(api, "GET /api/queries/stats")).toHaveLength(0);
    const show = within(first).getByRole("button", { name: en["settings_queries.show_cells"] });
    expect(show.getAttribute("aria-expanded")).toBe("false");
    await user.click(show);
    expect(show.getAttribute("aria-expanded")).toBe("true");
    const panel = document.getElementById(show.getAttribute("aria-controls") ?? "");
    expect(panel).not.toBeNull();
    const cells = await within(panel as HTMLElement).findAllByRole("listitem");
    expect(cells).toHaveLength(2);
    expect(cells[0]?.textContent).toContain(en["source.jsearch"]);
    expect(cells[0]?.textContent).toContain("Berlin");
    expect(cells[0]?.textContent).toContain(en["settings_queries.too_broad"]);
    expect(cells[0]?.textContent).toContain("3.5 days");
    expect(cells[1]?.textContent).toContain(en["source.serpapi"]);
    expect(cells[1]?.textContent).toContain(en["settings_queries.cell.remote"]);
    expect(cells[1]?.textContent).not.toContain(en["settings_queries.too_broad"]);
    // the other row's cells come from the same read
    const second = await rowOf("Data Engineer");
    await user.click(within(second).getByRole("button", { name: en["settings_queries.show_cells"] }));
    const other = document.getElementById(
      within(second).getByRole("button", { name: en["settings_queries.hide_cells"] }).getAttribute("aria-controls") ?? "",
    );
    const items = await within(other as HTMLElement).findAllByRole("listitem");
    expect(items).toHaveLength(1);
    expect(items[0]?.textContent).toContain("hamburg");
    expect(requestsTo(api, "GET /api/queries/stats")).toHaveLength(1);
    // collapsed again
    await user.click(within(first).getByRole("button", { name: en["settings_queries.hide_cells"] }));
    expect(within(first).getByRole("button", { name: en["settings_queries.show_cells"] }).getAttribute("aria-expanded")).toBe("false");
  });

  it("a cadence of 0 reads as a dash", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 1, refresh_cadence_days: 0 }), queryRow({ id: 2, text: "Data Engineer", refresh_cadence_days: null })];
    api.queryStats = [cellStats({ query_id: 1, cadence_days: 0 })];
    served(api, version(41, 12, { queries: [{ query_id: 1, enabled: true }, { query_id: 2, enabled: true }] }));
    renderSettings(["queries"]);
    const first = await rowOf("Backend Engineer");
    expect(cellOf(first, en["settings_queries.col.cadence"]).textContent).toBe(en["value.none"]);
    expect(cellOf(await rowOf("Data Engineer"), en["settings_queries.col.cadence"]).textContent).toBe(en["value.none"]);
    await userEvent.setup().click(within(first).getByRole("button", { name: en["settings_queries.show_cells"] }));
    const [cell] = await within(section()).findAllByRole("listitem");
    expect(cell.textContent).not.toContain("0 days");
    expect(within(cell).getByText(en["settings_queries.cell.cadence"]).nextElementSibling?.textContent).toBe(
      en["value.none"],
    );
  });

  it("Add enters the draft enabled", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 1 })];
    served(api, version(41, 12, { queries: [{ query_id: 1, enabled: true }] }));
    api.routes.set("POST /api/queries", (request) => {
      const body = JSON.parse(request.body ?? "{}") as { text: string; lang: "de" | "en" };
      api.queries = [...api.queries, queryRow({ id: 9, text: body.text, lang: body.lang, enabled: false })];
      return json({ id: 9, text: body.text, lang: body.lang, origin: "user", exploration: false, proposal_id: null }, 201);
    });
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    await table();
    const add = within(section()).getByRole("button", { name: en["settings_queries.add"] });
    const text = within(section()).getByRole("textbox", { name: en["settings_queries.new_text"] });
    await user.type(text, "D");
    expect((add as HTMLButtonElement).disabled).toBe(true); // 2 to 120 characters
    await user.type(text, "atenbank Entwickler");
    await user.selectOptions(within(section()).getByRole("combobox", { name: en["settings_queries.new_lang"] }), "de");
    const listReads = requestsTo(api, "GET /api/queries").length;
    await user.click(add);

    const [post] = requestsTo(api, "POST /api/queries");
    expect(JSON.parse(post.body ?? "")).toEqual({ text: "Datenbank Entwickler", lang: "de" });
    expect(post.headers.has("Idempotency-Key")).toBe(false);
    await waitFor(() => {
      expect(formOf(probe).draft.queries).toEqual([{ query_id: 1, enabled: true }, { query_id: 9, enabled: true }]);
    });
    expect([...formOf(probe).dirty]).toEqual(["queries[id=9]"]);
    expect(await within(section()).findByText("“Datenbank Entwickler” is added and enabled in your draft")).toBeTruthy();
    // the list is read again, and the new row is ticked from the draft
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/queries").length).toBeGreaterThan(listReads);
    });
    expect(checkbox(await rowOf("Datenbank Entwickler")).checked).toBe(true);
    expect(text).toHaveProperty("value", "");
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.queries).toEqual([{ query_id: 1, enabled: true }, { query_id: 9, enabled: true }]);
    });
    // the save is the panel's
    expect(saves(api)).toHaveLength(0);
  });

  it("adding an existing query enables it in the draft", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 1 }), queryRow({ id: 3, text: "Data Engineer" })];
    served(api, version(41, 12, { queries: [{ query_id: 1, enabled: true }] }));
    let existing = 3;
    api.routes.set("POST /api/queries", () => problem(409, "conflict_state", { query_id: existing }));
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    await table();
    const text = within(section()).getByRole("textbox", { name: en["settings_queries.new_text"] });
    await user.type(text, "data engineer");
    await user.click(within(section()).getByRole("button", { name: en["settings_queries.add"] }));
    expect(await within(section()).findByText(en["settings_queries.exists_enabled"])).toBeTruthy();
    expect(formOf(probe).draft.queries).toEqual([{ query_id: 1, enabled: true }, { query_id: 3, enabled: true }]);
    expect(checkbox(await rowOf("Data Engineer")).checked).toBe(true);
    // a query the draft holds already
    existing = 1;
    await user.clear(text);
    await user.type(text, "backend engineer");
    await user.click(within(section()).getByRole("button", { name: en["settings_queries.add"] }));
    expect(await within(section()).findByText(en["settings_queries.exists"])).toBeTruthy();
    expect(formOf(probe).draft.queries).toEqual([{ query_id: 1, enabled: true }, { query_id: 3, enabled: true }]);
    expect(within(section()).queryByRole("alert")).toBeNull();
  });

  it("Edit patches and rebases", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 1 })];
    const settings = served(api, version(41, 12, { queries: [{ query_id: 1, enabled: true }] }));
    api.routes.set("PATCH /api/queries/1", (request) => {
      const body = JSON.parse(request.body ?? "{}") as { text: string; lang: "de" | "en" };
      // the server's internal save moved the entry to the new id
      settings.current = version(42, 13, { queries: [{ query_id: 7, enabled: true }] });
      api.queries = [queryRow({ id: 7, text: body.text, lang: body.lang })];
      return json({ id: 7, text: body.text, lang: body.lang, origin: "user", exploration: false, proposal_id: null });
    });
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    await user.click(within(await rowOf("Backend Engineer")).getByRole("button", { name: en["settings_queries.edit"] }));
    const editor = within(section()).getByRole("group", { name: "Edit “Backend Engineer”" });
    const text = within(editor).getByRole("textbox", { name: en["settings_queries.edit_text"] });
    expect((text as HTMLInputElement).value).toBe("Backend Engineer");
    await user.clear(text);
    await user.type(text, "Backend Developer");
    const reads = requestsTo(api, "GET /api/settings").length;
    await user.click(within(editor).getByRole("button", { name: en["settings_queries.edit_save"] }));

    const [patch] = requestsTo(api, "PATCH /api/queries/1");
    expect(JSON.parse(patch.body ?? "")).toEqual({ text: "Backend Developer", lang: "en" });
    expect(patch.headers.has("Idempotency-Key")).toBe(false);
    await waitFor(() => {
      expect(formOf(probe).base.version.id).toBe(42);
    });
    expect(requestsTo(api, "GET /api/settings").length).toBeGreaterThan(reads);
    expect(formOf(probe).draft.queries).toEqual([{ query_id: 7, enabled: true }]);
    expect(formOf(probe).dirty.size).toBe(0);
    expect(checkbox(await rowOf("Backend Developer")).checked).toBe(true);
    expect(within(section()).queryByRole("group", { name: "Edit “Backend Engineer”" })).toBeNull();
    expect(saves(api)).toHaveLength(0);
  });

  it("an edit with unsaved changes keeps them after the rebase", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 1 }), queryRow({ id: 2, text: "Data Engineer" })];
    const queries = [{ query_id: 1, enabled: true }, { query_id: 2, enabled: true }];
    const settings = served(api, version(41, 12, { queries }));
    api.routes.set("PATCH /api/queries/1", () => {
      settings.current = version(42, 13, { queries: [{ query_id: 7, enabled: true }, { query_id: 2, enabled: true }] });
      api.queries = [queryRow({ id: 7, lang: "de" }), queryRow({ id: 2, text: "Data Engineer" })];
      return json({ id: 7, text: "Backend Engineer", lang: "de", origin: "user", exploration: false, proposal_id: null });
    });
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    // two unsaved changes: a source off, and the other query disabled
    await user.click(await within(section()).findByRole("checkbox", { name: en["source.arbeitnow"] }));
    await user.click(checkbox(await rowOf("Data Engineer")));
    expect([...formOf(probe).dirty].sort()).toEqual(["queries[id=2].enabled", "sources.enabled.arbeitnow"]);
    await user.click(within(await rowOf("Backend Engineer")).getByRole("button", { name: en["settings_queries.edit"] }));
    const editor = within(section()).getByRole("group", { name: "Edit “Backend Engineer”" });
    await user.selectOptions(within(editor).getByRole("combobox", { name: en["settings_queries.edit_lang"] }), "de");
    await user.click(within(editor).getByRole("button", { name: en["settings_queries.edit_save"] }));

    await waitFor(() => {
      expect(formOf(probe).base.version.id).toBe(42);
    });
    expect(JSON.parse(requestsTo(api, "PATCH /api/queries/1")[0]?.body ?? "")).toEqual({ text: "Backend Engineer", lang: "de" });
    expect(formOf(probe).draft.sources.enabled.arbeitnow).toBe(false);
    expect(formOf(probe).draft.queries).toEqual([{ query_id: 7, enabled: true }, { query_id: 2, enabled: false }]);
    expect([...formOf(probe).dirty].sort()).toEqual(["queries[id=2].enabled", "sources.enabled.arbeitnow"]);
    expect(screen.queryByText(en["settings_form.changed_elsewhere"])).toBeNull();
    await waitFor(() => {
      const draft = dryRunPayloads(api).at(-1);
      expect([draft?.queries, draft?.sources.enabled.arbeitnow]).toEqual([
        [{ query_id: 7, enabled: true }, { query_id: 2, enabled: false }],
        false,
      ]);
    });
    expect(saves(api)).toHaveLength(0);
  });

  it("an edit moves the row's draft entry to the new query", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 3, text: "Data Engineer", enabled: false })];
    served(api, version(41, 12)); // the settings list no query
    api.routes.set("PATCH /api/queries/3", () => {
      // no settings version names query 3: the edit saves no settings
      api.queries = [queryRow({ id: 8, text: "Data Engineers", enabled: false })];
      return json({ id: 8, text: "Data Engineers", lang: "en", origin: "user", exploration: false, proposal_id: null });
    });
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    await user.click(checkbox(await rowOf("Data Engineer")));
    expect(formOf(probe).draft.queries).toEqual([{ query_id: 3, enabled: true }]);
    await user.click(within(await rowOf("Data Engineer")).getByRole("button", { name: en["settings_queries.edit"] }));
    const editor = within(section()).getByRole("group", { name: "Edit “Data Engineer”" });
    await user.type(within(editor).getByRole("textbox", { name: en["settings_queries.edit_text"] }), "s");
    await user.click(within(editor).getByRole("button", { name: en["settings_queries.edit_save"] }));
    await waitFor(() => {
      expect(formOf(probe).draft.queries).toEqual([{ query_id: 8, enabled: true }]);
    });
    expect([...formOf(probe).dirty]).toEqual(["queries[id=8]"]);
    expect(checkbox(await rowOf("Data Engineers")).checked).toBe(true);
  });

  it("an edit's follow-up runs after the editor closes mid-flight", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 3, text: "Data Engineer", enabled: false })];
    served(api, version(41, 12)); // the settings list no query: the edit saves no settings
    let answer: (() => void) | null = null;
    api.routes.set("PATCH /api/queries/3", () =>
      new Promise<Response>((resolve) => {
        answer = () => {
          resolve(json({ id: 8, text: "Data Engineers", lang: "en", origin: "user", exploration: false, proposal_id: null }));
        };
      }),
    );
    const { probe, queryClient } = renderSettings(["queries"]);
    const user = userEvent.setup();
    await user.click(checkbox(await rowOf("Data Engineer")));
    await user.click(within(await rowOf("Data Engineer")).getByRole("button", { name: en["settings_queries.edit"] }));
    const editor = within(section()).getByRole("group", { name: "Edit “Data Engineer”" });
    await user.type(within(editor).getByRole("textbox", { name: en["settings_queries.edit_text"] }), "s");
    await user.click(within(editor).getByRole("button", { name: en["settings_queries.edit_save"] }));
    await waitFor(() => {
      expect(answer).not.toBeNull();
    });
    // while the PATCH is out, the editor cannot be closed by its own buttons
    expect(within(editor).getByRole<HTMLButtonElement>("button", { name: en["action.cancel"] }).disabled).toBe(true);
    expect(within(await rowOf("Data Engineer")).getByRole<HTMLButtonElement>("button", { name: en["settings_queries.edit"] }).disabled).toBe(true);
    // a list read drops the retired row, and its editor with it
    api.queries = [queryRow({ id: 8, text: "Data Engineers", enabled: false })];
    await act(() => queryClient.invalidateQueries({ queryKey: QUERIES_KEY }));
    await waitFor(() => {
      expect(within(section()).queryByRole("group", { name: "Edit “Data Engineer”" })).toBeNull();
    });
    await act(async () => {
      answer?.();
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(formOf(probe).draft.queries).toEqual([{ query_id: 8, enabled: true }]);
    });
    expect([...formOf(probe).dirty]).toEqual(["queries[id=8]"]);
    expect(checkbox(await rowOf("Data Engineers")).checked).toBe(true);
  });

  it("an edit carries the row's own toggle to the new query", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 1 })];
    const settings = served(api, version(41, 12, { queries: [{ query_id: 1, enabled: true }] }));
    api.routes.set("PATCH /api/queries/1", () => {
      // the internal save moved the entry, its saved flag kept
      settings.current = version(42, 13, { queries: [{ query_id: 7, enabled: true }] });
      api.queries = [queryRow({ id: 7, text: "Backend Developer" })];
      return json({ id: 7, text: "Backend Developer", lang: "en", origin: "user", exploration: false, proposal_id: null });
    });
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    await user.click(checkbox(await rowOf("Backend Engineer"))); // disabled in the draft
    await user.click(within(await rowOf("Backend Engineer")).getByRole("button", { name: en["settings_queries.edit"] }));
    const editor = within(section()).getByRole("group", { name: "Edit “Backend Engineer”" });
    const text = within(editor).getByRole("textbox", { name: en["settings_queries.edit_text"] });
    await user.clear(text);
    await user.type(text, "Backend Developer");
    await user.click(within(editor).getByRole("button", { name: en["settings_queries.edit_save"] }));
    await waitFor(() => {
      expect(formOf(probe).base.version.id).toBe(42);
    });
    await waitFor(() => {
      expect(formOf(probe).draft.queries).toEqual([{ query_id: 7, enabled: false }]);
    });
    expect([...formOf(probe).dirty]).toEqual(["queries[id=7].enabled"]);
    expect(checkbox(await rowOf("Backend Developer")).checked).toBe(false);
  });

  it("a toggle of the row made while its edit is out wins", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 1 })];
    const settings = served(api, version(41, 12, { queries: [{ query_id: 1, enabled: true }] }));
    let answer: (() => void) | null = null;
    api.routes.set("PATCH /api/queries/1", () =>
      new Promise<Response>((resolve) => {
        answer = () => {
          settings.current = version(42, 13, { queries: [{ query_id: 7, enabled: true }] });
          api.queries = [queryRow({ id: 7, text: "Backend Developer" })];
          resolve(json({ id: 7, text: "Backend Developer", lang: "en", origin: "user", exploration: false, proposal_id: null }));
        };
      }),
    );
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    await user.click(within(await rowOf("Backend Engineer")).getByRole("button", { name: en["settings_queries.edit"] }));
    const editor = within(section()).getByRole("group", { name: "Edit “Backend Engineer”" });
    const text = within(editor).getByRole("textbox", { name: en["settings_queries.edit_text"] });
    await user.clear(text);
    await user.type(text, "Backend Developer");
    await user.click(within(editor).getByRole("button", { name: en["settings_queries.edit_save"] }));
    await waitFor(() => {
      expect(answer).not.toBeNull();
    });
    await user.click(checkbox(await rowOf("Backend Engineer"))); // disabled while the PATCH is out
    await act(async () => {
      answer?.();
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(formOf(probe).base.version.id).toBe(42);
    });
    await waitFor(() => {
      expect(formOf(probe).draft.queries).toEqual([{ query_id: 7, enabled: false }]);
    });
  });

  it("an edit whose rebase fails moves no entry", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 1 })];
    served(api, version(41, 12, { queries: [{ query_id: 1, enabled: true }] }));
    api.routes.set("PATCH /api/queries/1", () => {
      // the route saved version 42 with query 7, which the form cannot read
      api.routes.set("GET /api/settings", () => problem(503, "internal"));
      api.queries = [queryRow({ id: 7, text: "Backend Developer" })];
      return json({ id: 7, text: "Backend Developer", lang: "en", origin: "user", exploration: false, proposal_id: null });
    });
    const { probe } = renderSettings(["queries"], { retryDelay: 1 });
    const user = userEvent.setup();
    await user.click(checkbox(await rowOf("Backend Engineer")));
    await user.click(checkbox(await rowOf("Backend Engineer"))); // toggled and back: the row's entry is the base's
    await user.click(within(await rowOf("Backend Engineer")).getByRole("button", { name: en["settings_queries.edit"] }));
    const editor = within(section()).getByRole("group", { name: "Edit “Backend Engineer”" });
    const text = within(editor).getByRole("textbox", { name: en["settings_queries.edit_text"] });
    await user.clear(text);
    await user.type(text, "Backend Developer");
    await user.click(within(editor).getByRole("button", { name: en["settings_queries.edit_save"] }));
    // the follow-up ended: the list was read again
    expect(await within(section()).findByText("Backend Developer", {}, { timeout: 3000 })).toBeTruthy();
    expect(formOf(probe).base.version.id).toBe(41);
    expect(formOf(probe).draft.queries).toEqual([{ query_id: 1, enabled: true }]);
    expect(formOf(probe).dirty.size).toBe(0);
  });

  it("Disable changes the draft", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 1 })];
    served(api, version(41, 12, { queries: [{ query_id: 1, enabled: true }] }));
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    const box = checkbox(await rowOf("Backend Engineer"));
    expect(box.getAttribute("aria-label")).toBe("Enable “Backend Engineer”");
    await user.click(box);
    expect(box.checked).toBe(false);
    expect(formOf(probe).draft.queries).toEqual([{ query_id: 1, enabled: false }]);
    expect([...formOf(probe).dirty]).toEqual(["queries[id=1].enabled"]);
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.queries).toEqual([{ query_id: 1, enabled: false }]);
    });
    expect(writes(api)).toEqual([]);
  });

  it("a query missing from the settings can be enabled into the draft", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 1 }), queryRow({ id: 3, text: "Data Engineer", enabled: false })];
    served(api, version(41, 12, { queries: [{ query_id: 1, enabled: true }] }));
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    const row = await rowOf("Data Engineer");
    expect(within(row).getByText(en["settings_entries.not_in_settings"])).toBeTruthy();
    expect(checkbox(row).checked).toBe(false);
    expect(within(await rowOf("Backend Engineer")).queryByText(en["settings_entries.not_in_settings"])).toBeNull();
    await user.click(checkbox(row));
    expect(checkbox(row).checked).toBe(true);
    expect(within(row).queryByText(en["settings_entries.not_in_settings"])).toBeNull();
    expect(formOf(probe).draft.queries).toEqual([{ query_id: 1, enabled: true }, { query_id: 3, enabled: true }]);
    expect([...formOf(probe).dirty]).toEqual(["queries[id=3]"]);
    // unticked again, the draft is the saved settings
    await user.click(checkbox(row));
    expect(formOf(probe).draft.queries).toEqual([{ query_id: 1, enabled: true }]);
    expect(formOf(probe).dirty.size).toBe(0);
    expect(within(row).getByText(en["settings_entries.not_in_settings"])).toBeTruthy();
    expect(writes(api)).toEqual([]);
  });

  it("an emptied stale period ticks its box again when it loses the focus", async () => {
    const api = fakeApi({ signedIn: true });
    const base = settingsRead().payload;
    served(api, version(41, 12, { collection: { ...base.collection, stale_ttl_days: 45 } }));
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    const follow = await within(section()).findByRole<HTMLInputElement>("checkbox", { name: "Same as the maximum ad age (30 days)" });
    expect(follow.checked).toBe(false);
    const ttl = within(section()).getByRole<HTMLInputElement>("spinbutton", { name: en["settings_queries.stale_ttl"] });
    expect(ttl.value).toBe("45");
    await user.clear(ttl);
    // emptied, the number stays while it has the focus; the draft follows the maximum ad age
    expect(within(section()).getByRole("spinbutton", { name: en["settings_queries.stale_ttl"] })).toBe(ttl);
    expect(follow.checked).toBe(false);
    expect(formOf(probe).draft.collection.stale_ttl_days).toBeNull();
    await user.tab();
    expect(follow.checked).toBe(true);
    expect(within(section()).queryByRole("spinbutton", { name: en["settings_queries.stale_ttl"] })).toBeNull();
    expect([...formOf(probe).dirty]).toEqual(["collection.stale_ttl_days"]);
  });

  it("an emptied stale period that is the saved one is clean and ticked", async () => {
    const api = fakeApi({ signedIn: true });
    served(api, version(41, 12)); // stale_ttl_days null: the maximum ad age
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    const follow = await within(section()).findByRole<HTMLInputElement>("checkbox", { name: "Same as the maximum ad age (30 days)" });
    await user.click(follow);
    const ttl = within(section()).getByRole<HTMLInputElement>("spinbutton", { name: en["settings_queries.stale_ttl"] });
    expect(ttl.value).toBe("30");
    await user.click(ttl);
    await user.clear(ttl);
    expect(formOf(probe).dirty.size).toBe(0);
    await user.tab();
    expect(follow.checked).toBe(true);
    expect(within(section()).queryByRole("spinbutton", { name: en["settings_queries.stale_ttl"] })).toBeNull();
    expect(await within(section()).findByText(en["settings_form.no_changes"])).toBeTruthy();
  });

  it("the table keeps its roles in every layout", async () => {
    const api = fakeApi({ signedIn: true });
    api.queries = [queryRow({ id: 1 })];
    served(api, version(41, 12, { queries: [{ query_id: 1, enabled: true }] }));
    renderSettings(["queries"]);
    const grid = await table();
    // explicit roles: a card layout's `display: block` keeps them in every engine
    expect(grid.getAttribute("role")).toBe("table");
    expect(within(grid).getAllByRole("rowgroup").map((group) => group.getAttribute("role"))).toEqual(["rowgroup", "rowgroup"]);
    const heads = within(grid).getAllByRole("columnheader");
    expect(heads).toHaveLength(11);
    expect(heads.every((head) => head.getAttribute("role") === "columnheader")).toBe(true);
    const row = await rowOf("Backend Engineer");
    expect(row.getAttribute("role")).toBe("row");
    const cells = within(row).getAllByRole("cell");
    expect(cells).toHaveLength(11);
    expect(cells.every((cell) => cell.getAttribute("role") === "cell")).toBe(true);
  });

  it("binds the source switches, the quota limits and the stale period to the draft", async () => {
    const api = fakeApi({ signedIn: true });
    served(api, version(41, 12));
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    await user.click(await within(section()).findByRole("checkbox", { name: en["settings_queries.source_ats"] }));
    expect(formOf(probe).draft.sources.enabled.ats).toBe(false);
    const limit = within(section()).getByRole("spinbutton", { name: en["settings_queries.quota.jsearch_monthly_limit"] });
    await user.clear(limit);
    await user.type(limit, "150");
    expect(formOf(probe).draft.quota.jsearch_monthly_limit).toBe(150);
    // the paid bound: automatic, or a number
    const auto = within(section()).getByRole("checkbox", { name: en["settings_queries.quota.paid_auto"] });
    expect((auto as HTMLInputElement).checked).toBe(true);
    expect(within(section()).queryByRole("spinbutton", { name: en["settings_queries.quota.paid_max"] })).toBeNull();
    await user.click(auto);
    expect(formOf(probe).draft.quota.paid_max_requests_per_run).toBe(10);
    const paid = within(section()).getByRole("spinbutton", { name: en["settings_queries.quota.paid_max"] });
    await user.clear(paid);
    await user.type(paid, "25");
    expect(formOf(probe).draft.quota.paid_max_requests_per_run).toBe(25);
    await user.click(auto);
    expect(formOf(probe).draft.quota.paid_max_requests_per_run).toBe("auto");
    // the stale period follows the maximum ad age until it is set
    const follow = within(section()).getByRole("checkbox", { name: "Same as the maximum ad age (30 days)" });
    expect((follow as HTMLInputElement).checked).toBe(true);
    await user.click(follow);
    expect(formOf(probe).draft.collection.stale_ttl_days).toBe(30);
    const ttl = within(section()).getByRole("spinbutton", { name: en["settings_queries.stale_ttl"] });
    await user.clear(ttl);
    await user.type(ttl, "45");
    expect(formOf(probe).draft.collection.stale_ttl_days).toBe(45);
    await user.click(follow);
    expect(formOf(probe).draft.collection.stale_ttl_days).toBeNull();
    expect([...formOf(probe).dirty].sort()).toEqual(["quota.jsearch_monthly_limit", "sources.enabled.ats"]);
  });
});
