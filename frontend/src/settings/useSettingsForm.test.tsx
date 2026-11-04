import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SETTINGS_KEY } from "../app/AppShell";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import {
  type FakeApi,
  type FakeRequest,
  dryRunOut,
  fakeApi,
  json,
  problem,
  projectionsOut,
  requestsTo,
  settingsRead,
} from "../test/api";
import { dryRunPayloads, formOf, renderSettings, saveBody, saves } from "../test/settings";

type Settings = components["schemas"]["Settings"];
type SettingsOut = components["schemas"]["SettingsOut"];
type DryRunOut = components["schemas"]["DryRunOut"];

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SLOW = { code: "paid_refresh_slow", detail: { cadence_days: 9.5 } };

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

/** Settings version `id` (number `no`) of `payload`. */
function version(id: number, no: number, payload: Settings, pending: SettingsOut["pending_scope"] = null): SettingsOut {
  return {
    version: { id, version_no: no, created_at: "2026-11-03T06:00:00Z", effect_classes: [] },
    payload,
    pending_scope: pending,
    scope_applied: pending === null,
  };
}

/** The settings the stub answers, which a test changes as a save elsewhere would. */
function served(api: FakeApi, first: SettingsOut = settingsRead()): { current: SettingsOut } {
  const state = { current: first };
  api.routes.set("GET /api/settings", () => json(state.current));
  return state;
}

/** Answers each dry-run by its document: the base's as `base`, any other by `draft`. */
function dryRuns(api: FakeApi, draft: (payload: Settings) => DryRunOut | Promise<DryRunOut>, base: () => DryRunOut = () => dryRunOut()) {
  api.routes.set("POST /api/settings/dry-run", async (request: FakeRequest) => {
    const { payload } = JSON.parse(request.body ?? "{}") as { payload: Settings };
    const current = JSON.parse(JSON.stringify(settingsRead().payload)) as Settings;
    const isBase = JSON.stringify(payload) === JSON.stringify(current);
    return json(isBase ? base() : await draft(payload));
  });
}

async function intervalField(): Promise<HTMLInputElement> {
  return (await screen.findByRole<HTMLInputElement>("spinbutton", { name: en["settings_schedule.interval"] }));
}

function saveButton(): HTMLButtonElement {
  return screen.getByRole<HTMLButtonElement>("button", { name: en["settings_form.save"] });
}

/** The warning of `code` in the panel (its item). */
function warningItem(code: keyof typeof en): HTMLElement {
  const item = screen.getByText(en[code]).closest("li");
  if (item === null) {
    throw new Error(`no warning ${code}`);
  }
  return item;
}

async function typeInterval(value: string): Promise<void> {
  fireEvent.change(await intervalField(), { target: { value } });
}

function saved(id: number, no: number) {
  return json({ version: { id, version_no: no, effect_classes: ["schedule"] }, apply_scope_operation_id: null }, 201);
}

describe("useSettingsForm", () => {
  it("save posts the whole payload with the base version, the ticks and a key", async () => {
    const api = fakeApi({ signedIn: true });
    const settings = served(api);
    dryRuns(api, () => dryRunOut({ warnings: [SLOW], effects: ["schedule"] }));
    api.routes.set("POST /api/settings", (request) => {
      settings.current = version(42, 13, saveBody(request).payload);
      return saved(42, 13);
    });
    renderSettings(["schedule"]);
    await typeInterval("8");
    const understand = await within(await waitFor(() => warningItem("warning.paid_refresh_slow"))).findByRole("checkbox", {
      name: en["settings_form.understand"],
    });
    expect(saveButton().disabled).toBe(true); // the warning is not ticked yet
    await userEvent.setup().click(understand);
    expect(saveButton().disabled).toBe(false);
    await userEvent.setup().click(saveButton());

    await waitFor(() => {
      expect(saves(api)).toHaveLength(1);
    });
    const [request] = saves(api);
    const expected = { ...settingsRead().payload, schedule: { ...settingsRead().payload.schedule, interval_hours: 8 } };
    expect(saveBody(request)).toEqual({
      payload: expected,
      base_version_id: 41,
      acknowledged_warnings: ["paid_refresh_slow"],
    });
    expect(request.headers.get("Idempotency-Key")).toMatch(UUID);
    expect(request.headers.get("X-JH-Request")).toBe("1");
    expect(await screen.findByText("Settings saved (v13)")).toBeTruthy();
    // the new version is read, and the form is clean on it
    expect(await screen.findByText(en["settings_form.no_changes"])).toBeTruthy();
    expect((await intervalField()).value).toBe("8");
    expect(requestsTo(api, "GET /api/settings").length).toBeGreaterThanOrEqual(2);
  });

  it("a stale base offers reload and reapply and keeps the dirty fields", async () => {
    const api = fakeApi({ signedIn: true });
    const settings = served(api);
    // the warning comes with the draft's interval only, so it is never a standing one of a base
    dryRuns(api, (payload) => dryRunOut({ warnings: payload.schedule.interval_hours === 8 ? [SLOW] : [], effects: ["schedule"] }));
    api.routes.set("POST /api/settings", () => problem(409, "stale_base_version"));
    renderSettings(["schedule"]);
    await typeInterval("8");
    const understand = await within(await waitFor(() => warningItem("warning.paid_refresh_slow"))).findByRole("checkbox", {
      name: en["settings_form.understand"],
    });
    await userEvent.setup().click(understand);
    // another tab saved version 13 meanwhile, with a longer ad age
    const elsewhere = settingsRead().payload;
    settings.current = version(42, 13, { ...elsewhere, scope: { ...elsewhere.scope, max_ad_age_days: 45 } });
    await userEvent.setup().click(saveButton());

    expect(await screen.findByText(en["settings_form.changed_elsewhere"])).toBeTruthy();
    expect(saveButton().disabled).toBe(true);
    const before = dryRunPayloads(api).length;
    await userEvent.setup().click(screen.getByRole("button", { name: en["settings_form.reload_and_reapply"] }));

    // the new base's own dry-run, then the draft's: the dirty field written onto the new base
    await waitFor(() => {
      expect(dryRunPayloads(api).slice(before).map((p) => [p.schedule.interval_hours, p.scope.max_ad_age_days])).toEqual([
        [6, 45],
        [8, 45],
      ]);
    });
    expect(screen.queryByText(en["settings_form.changed_elsewhere"])).toBeNull();
    expect((await intervalField()).value).toBe("8");
    // the ticks were cleared: the warning needs a new tick
    const again = within(await waitFor(() => warningItem("warning.paid_refresh_slow"))).getByRole<HTMLInputElement>("checkbox", {
      name: en["settings_form.understand"],
    });
    expect(again.checked).toBe(false);
    await userEvent.setup().click(again);
    api.routes.set("POST /api/settings", () => saved(43, 14));
    await userEvent.setup().click(saveButton());
    await waitFor(() => {
      expect(saves(api)).toHaveLength(2);
    });
    expect(saveBody(saves(api)[1]).base_version_id).toBe(42);
  });

  it("the dry run is debounced 500 ms", async () => {
    const api = fakeApi({ signedIn: true });
    served(api);
    renderSettings(["schedule"]);
    const interval = await intervalField();
    await waitFor(() => {
      expect(dryRunPayloads(api)).toHaveLength(1); // the base document's, once on load
    });
    vi.useFakeTimers();
    fireEvent.change(interval, { target: { value: "8" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    fireEvent.change(interval, { target: { value: "9" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(499);
    });
    expect(dryRunPayloads(api)).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(dryRunPayloads(api)).toHaveLength(2);
    expect(dryRunPayloads(api)[1].schedule.interval_hours).toBe(9);
    // back to the base: no dry-run
    fireEvent.change(interval, { target: { value: "6" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(dryRunPayloads(api)).toHaveLength(2);
    expect(screen.getByText(en["settings_form.no_changes"])).toBeTruthy();
  });

  it("rebase after an internal save keeps the dirty fields", async () => {
    const api = fakeApi({ signedIn: true });
    const settings = served(api);
    const { probe } = renderSettings(["schedule"]);
    await typeInterval("8");
    // a watchlist add saved version 13 (an internal save of another route)
    const base = settingsRead().payload;
    settings.current = version(42, 13, { ...base, watchlist: [{ watchlist_company_id: 7, enabled: true }] });
    await act(async () => {
      await formOf(probe).rebase();
    });
    expect(formOf(probe).base.version.id).toBe(42);
    expect([...formOf(probe).dirty]).toEqual(["schedule.interval_hours"]);
    expect(formOf(probe).draft.watchlist).toEqual([{ watchlist_company_id: 7, enabled: true }]);
    expect(screen.queryByText(en["settings_form.changed_elsewhere"])).toBeNull();
    await waitFor(() => {
      const draft = dryRunPayloads(api).at(-1);
      expect([draft?.watchlist, draft?.schedule.interval_hours]).toEqual([[{ watchlist_company_id: 7, enabled: true }], 8]);
    });
    expect((await intervalField()).value).toBe("8");
  });

  it("a rebase keeps an entry's toggle by id after the list changed", async () => {
    const api = fakeApi({ signedIn: true });
    const base = settingsRead().payload;
    const settings = served(
      api,
      version(41, 12, {
        ...base,
        queries: [{ query_id: 1, enabled: true }, { query_id: 2, enabled: true }],
        watchlist: [{ watchlist_company_id: 5, enabled: true }],
      }),
    );
    const { probe } = renderSettings(["schedule"]);
    await intervalField();
    act(() => {
      formOf(probe).setEntry("queries", 2, false);
      formOf(probe).setEntry("queries", 1, false);
      formOf(probe).addEntry("watchlist", 9);
    });
    expect(formOf(probe).draft.queries).toEqual([{ query_id: 1, enabled: false }, { query_id: 2, enabled: false }]);
    // query 1 was removed and query 3 added before query 2; company 5 removed, 6 added
    settings.current = version(42, 13, {
      ...base,
      queries: [{ query_id: 3, enabled: true }, { query_id: 2, enabled: true }],
      watchlist: [{ watchlist_company_id: 6, enabled: true }],
    });
    await act(async () => {
      await formOf(probe).rebase();
    });
    expect(formOf(probe).draft.queries).toEqual([{ query_id: 3, enabled: true }, { query_id: 2, enabled: false }]);
    expect(formOf(probe).draft.watchlist).toEqual([
      { watchlist_company_id: 6, enabled: true },
      { watchlist_company_id: 9, enabled: true },
    ]);
    expect([...formOf(probe).dirty].sort()).toEqual(["queries[id=2].enabled", "watchlist[id=9]"]);
    await waitFor(() => {
      expect(dryRunPayloads(api).map((p) => p.queries)).toContainEqual([{ query_id: 3, enabled: true }, { query_id: 2, enabled: false }]);
    });
    // toggled back to the base's value, the entry is clean again
    act(() => {
      formOf(probe).setEntry("queries", 2, true);
    });
    expect([...formOf(probe).dirty]).toEqual(["watchlist[id=9]"]);
  });

  it("a background refetch with a newer version keeps the dirty draft and offers the reload", async () => {
    const api = fakeApi({ signedIn: true });
    const settings = served(api);
    const { queryClient } = renderSettings(["schedule"]);
    await typeInterval("8");
    const base = settingsRead().payload;
    settings.current = version(42, 13, { ...base, scope: { ...base.scope, max_ad_age_days: 45 } });
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: SETTINGS_KEY });
    });
    expect(await screen.findByText(en["settings_form.changed_elsewhere"])).toBeTruthy();
    expect((await intervalField()).value).toBe("8");
    // the same version again changes nothing
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: SETTINGS_KEY });
    });
    expect((await intervalField()).value).toBe("8");
    // once nothing is dirty, a newer version is taken silently
    await typeInterval("6");
    await waitFor(() => {
      expect(screen.queryByText(en["settings_form.changed_elsewhere"])).toBeNull();
    });
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.scope.max_ad_age_days).toBe(45); // the new base's own dry-run
    });
    expect(screen.getByText(en["settings_form.no_changes"])).toBeTruthy();
  });

  it("a late dry-run answer never replaces a newer one", async () => {
    const api = fakeApi({ signedIn: true });
    served(api);
    let release: (() => void) | null = null;
    dryRuns(api, (payload) => {
      if (payload.schedule.interval_hours === 8) {
        return new Promise((resolve) => {
          release = () => { resolve(dryRunOut({ warnings: [SLOW], effects: ["schedule"] })); };
        });
      }
      return dryRunOut({ effects: ["schedule"] });
    });
    renderSettings(["schedule"]);
    await typeInterval("8");
    await waitFor(() => {
      expect(release).not.toBeNull();
    });
    await typeInterval("9");
    await waitFor(() => {
      expect(saveButton().disabled).toBe(false); // the answer for 9: no warning
    });
    await act(async () => {
      release?.();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(screen.queryByText(en["warning.paid_refresh_slow"])).toBeNull();
    expect(saveButton().disabled).toBe(false);
    expect(screen.queryByText(en["settings_form.checking"])).toBeNull();
  });

  it("Save is disabled while the dry-run runs", async () => {
    const api = fakeApi({ signedIn: true });
    served(api);
    let release: (() => void) | null = null;
    dryRuns(api, () => new Promise((resolve) => {
      release = () => { resolve(dryRunOut({ effects: ["schedule"] })); };
    }));
    renderSettings(["schedule"]);
    await typeInterval("8");
    expect(screen.getByText(en["settings_form.checking"])).toBeTruthy(); // before the 500 ms are up
    expect(saveButton().disabled).toBe(true);
    await waitFor(() => {
      expect(release).not.toBeNull();
    });
    expect(screen.getByText(en["settings_form.checking"])).toBeTruthy(); // and while it is read
    expect(saveButton().disabled).toBe(true);
    await act(async () => {
      release?.();
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(saveButton().disabled).toBe(false);
    });
    expect(screen.queryByText(en["settings_form.checking"])).toBeNull();
  });

  it("a tick is dropped when its warning leaves", async () => {
    const api = fakeApi({ signedIn: true });
    served(api);
    dryRuns(api, (payload) => dryRunOut({ warnings: payload.schedule.interval_hours === 9 ? [] : [SLOW], effects: ["schedule"] }));
    renderSettings(["schedule"]);
    await typeInterval("8");
    const understand = await within(await waitFor(() => warningItem("warning.paid_refresh_slow"))).findByRole("checkbox", {
      name: en["settings_form.understand"],
    });
    await userEvent.setup().click(understand);
    expect(saveButton().disabled).toBe(false);
    await typeInterval("9");
    await waitFor(() => {
      expect(screen.queryByText(en["warning.paid_refresh_slow"])).toBeNull();
    });
    await typeInterval("10");
    const again = within(await waitFor(() => warningItem("warning.paid_refresh_slow"))).getByRole<HTMLInputElement>("checkbox", {
      name: en["settings_form.understand"],
    });
    expect(again.checked).toBe(false);
    expect(saveButton().disabled).toBe(true);
  });

  it("eval_gate_failed offers the override and resends with a new key", async () => {
    const api = fakeApi({ signedIn: true });
    served(api);
    dryRuns(api, () => dryRunOut({ effects: ["evaluation_policy"], projections: projectionsOut({ eval_gate: "failed" }) }));
    api.routes.set("POST /api/settings", () =>
      problem(409, "eval_gate_failed", { detail: "acknowledge the override (eval_override_ack) to activate this change" }),
    );
    renderSettings(["schedule"]);
    await typeInterval("8");
    await waitFor(() => {
      expect(saveButton().disabled).toBe(false);
    });
    await userEvent.setup().click(saveButton());
    const override = (await screen.findByRole<HTMLInputElement>("checkbox", { name: en["settings_form.eval_override"] }));
    expect(screen.getByText("acknowledge the override (eval_override_ack) to activate this change")).toBeTruthy();
    expect(override.checked).toBe(false);
    await userEvent.setup().click(override);
    api.routes.set("POST /api/settings", () => saved(42, 13));
    await userEvent.setup().click(saveButton());
    await waitFor(() => {
      expect(saves(api)).toHaveLength(2);
    });
    const [first, second] = saves(api);
    expect(saveBody(first).eval_override_ack).toBeUndefined();
    expect(saveBody(second).eval_override_ack).toBe(true);
    expect(second.headers.get("Idempotency-Key")).toMatch(UUID);
    expect(second.headers.get("Idempotency-Key")).not.toBe(first.headers.get("Idempotency-Key"));
  });

  it("the scope poll never replaces the draft", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = fakeApi({ signedIn: true });
    const progress = { done: 10, total: 100, det_done: 0, det_total: 0 };
    const settings = served(api, version(41, 12, settingsRead().payload, { version_id: 9, progress }));
    renderSettings(["schedule"]);
    await typeInterval("8");
    const reads = () => requestsTo(api, "GET /api/settings");
    const first = reads().length;
    settings.current = version(41, 12, settingsRead().payload, { version_id: 9, progress: { ...progress, done: 60 } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    await waitFor(() => {
      expect(reads().length).toBeGreaterThan(first);
    });
    const polls = reads().slice(first);
    expect(polls.every((r) => r.headers.get("X-JH-Background") === "1")).toBe(true);
    expect((await intervalField()).value).toBe("8");
    expect(screen.queryByText(en["settings_form.no_changes"])).toBeNull();
    // the apply ends: no more polls
    settings.current = version(41, 12, settingsRead().payload);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    await waitFor(() => {
      expect(reads().at(-1)?.headers.get("X-JH-Background")).toBe("1");
    });
    const ended = reads().length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });
    expect(reads()).toHaveLength(ended);
    expect((await intervalField()).value).toBe("8");
    expect(screen.queryByText(en["settings_form.changed_elsewhere"])).toBeNull();
  });
});
