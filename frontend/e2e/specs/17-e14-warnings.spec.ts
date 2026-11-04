// E14 (spec §18.7, §13.5, §15.7; plan M4 Task 34, resolution f): forty queries of fixed texts, created through
// the API (or found again by their texts: a second create of a live query's text is 409 with its id). The
// settings hold at most forty query entries (`settings/model.py`), so one save through the API lists the forty,
// not enabled, in place of the setup's own query (which is then unlisted, so not enabled). Then one draft in the
// UI: the forty enabled in Queries & sources and the interval set to 1 h in Schedule. The dry-run's warnings
// appear, new to the draft, each with "I understand"; Save stays disabled until each is ticked, then answers 201.
// At the end one save through the API restores the interval and the query entries read at the start: the forty
// stay, unretired, and are not enabled (a live query the settings do not list is disabled).
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { answerTo, type ApiContext, apiContext, getJson } from "../support/api";
import { en, expect, filled, test } from "../support/fixtures";
import { readSettings, saveSettings, withRestore } from "../support/settings";

type QueryOut = components["schemas"]["QueryOut"];
type QueryListOut = components["schemas"]["QueryListOut"];

const QUERIES = 40; // the settings' most query entries
const INTERVAL_HOURS = 1;

/** The `n`-th query's fixed text (1 to 40). */
function queryText(n: number): string {
  return `e2e warning query ${String(n).padStart(2, "0")}`;
}

/** The forty queries' ids, in order: each created, or the live one of its text. */
async function fortyQueries(api: ApiContext): Promise<number[]> {
  const ids: number[] = [];
  for (let n = 1; n <= QUERIES; n++) {
    const response = await api.post("/api/queries", { data: { text: queryText(n), lang: "en" } });
    if (response.status() === 201) {
      ids.push(((await response.json()) as QueryOut).id);
      continue;
    }
    const problem = (await response.json()) as { code?: string; query_id?: number };
    if (response.status() !== 409 || problem.code !== "conflict_state" || typeof problem.query_id !== "number") {
      throw new Error(`the create of "${queryText(n)}" answered ${String(response.status())} (${String(problem.code)})`);
    }
    ids.push(problem.query_id);
  }
  return ids;
}

test("E14: forty queries at a 1 h interval raise warnings, and Save waits for each I understand", async ({ page }) => {
  test.setTimeout(300_000);
  const api = await apiContext(page);
  try {
    const before = (await readSettings(api)).payload;
    const ids = await fortyQueries(api);
    const restore = async () => {
      const what = `the interval back to ${String(before.schedule.interval_hours)} h and the query entries read at the start`;
      const restored = await saveSettings(api, what, (p) => {
        p.schedule.interval_hours = before.schedule.interval_hours;
        p.queries = before.queries;
      });
      expect(restored.response.status(), what).toBe(201);
    };
    await withRestore(async () => {
      const listed = await saveSettings(api, "the forty queries listed, not enabled", (p) => {
        p.queries = ids.map((query_id) => ({ query_id, enabled: false }));
      });
      expect(listed.response.status(), "the save that lists the forty queries").toBe(201);

      await page.goto("/settings#queries");
      const queries = page.getByRole("region", { name: en["settings.section.queries"] });
      for (let n = 1; n <= QUERIES; n++) {
        await queries.getByRole("checkbox", { name: filled(en["settings_queries.enable"], { text: queryText(n) }), exact: true }).check();
      }
      const schedule = page.getByRole("region", { name: en["settings.section.schedule"] });
      await schedule.getByLabel(en["settings_schedule.interval"], { exact: true }).fill(String(INTERVAL_HOURS));
      const warnings = schedule.getByRole("list", { name: en["settings_form.warnings"] });
      const understood = warnings.getByRole("checkbox", { name: en["settings_form.understand"], exact: true });
      await expect(understood.first()).toBeVisible();
      const save = schedule.getByRole("button", { name: en["settings_form.save"], exact: true });
      await expect(save).toBeDisabled();
      await expect(save).toHaveAccessibleDescription(en["settings_form.blocked_warnings"]);
      await expectNoSeriousA11yViolations(page);
      const ticks = await warnings.getByRole("checkbox").all();
      const fresh = await understood.count();
      test.info().annotations.push({ type: "warnings", description: `${String(ticks.length)} warnings, ${String(fresh)} with "I understand"` });
      expect(fresh, "warnings new to the draft").toBeGreaterThan(0);
      for (const [i, tick] of ticks.entries()) {
        await expect(save, `Save before tick ${String(i + 1)} of ${String(ticks.length)}`).toBeDisabled();
        await tick.check();
      }
      await expect(save).toBeEnabled();
      await expectNoSeriousA11yViolations(page);

      const answered = answerTo(page, "POST", "/api/settings");
      await save.click();
      const response = await answered;
      expect(response.status(), "the save of the forty queries at 1 h").toBe(201);
      const saved = (await readSettings(api)).payload;
      expect(saved.schedule.interval_hours).toBe(INTERVAL_HOURS);
      expect(saved.queries.filter((entry) => entry.enabled).map((entry) => entry.query_id)).toEqual(ids);
      await expect(schedule.getByText(en["settings_form.no_changes"], { exact: true })).toBeVisible();
    }, restore);
    const after = (await readSettings(api)).payload;
    expect([after.schedule.interval_hours, after.queries]).toEqual([before.schedule.interval_hours, before.queries]);
    const live = await getJson<QueryListOut[]>(api, "/api/queries");
    const forty = live.filter((query) => ids.includes(query.id));
    expect(forty.length, "the forty queries stay").toBe(QUERIES);
    expect(forty.filter((query) => query.enabled).map((query) => query.id), "enabled among the forty").toEqual([]);
  } finally {
    await api.dispose();
  }
});
