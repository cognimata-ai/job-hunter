// E7 (spec §18.7, §15.3, §8.12; plan M4 Task 34, Ruling T34-a): the Jobs page's filters, each check from the
// default filters ([Reset] between the numbered checks), each with a named card, and the list read back through
// the API under the page's own filters where the check is about every listed card:
// 1. the keyword "Entwickler" lists the Adzuna card of native id 5929169784, titled "Java-Entwickler" (the ad's
//    "Java-Entwickler (m/w/d)" with its gender tag stripped, §8.2);
// 2. Berlin (the setup's, at its default 30 km) added keeps it listed;
// 3. work mode "unknown" unticked removes it (Adzuna gives no work mode, and an unknown mode matches only
//    "unknown"), and ticked again brings it back;
// 7. a reload of check 3's final state keeps the URL and every filter control;
// 4. work mode "remote" alone lists the Ashby n8n card "Sr Security Engineer | Remote | Europe", and every listed
//    job has a known work mode that includes remote;
// 5. posted within 7 days: every listed job's posted date (its first-seen date when unknown) is within 7 days,
//    or, for a group's card, one of its deferred members' (a group is listed when one of them matches, §8.7);
// 6. contested only: each listed card has the contested badge; scoring state "unscored / pending" alone: each
//    has a state chip and no score.
// A list read once is the query cache's afterwards (Reset, a box ticked again): the page shows it unread.
import type { Locator, Page } from "@playwright/test";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { apiContext, getJson } from "../support/api";
import { en, expect, test } from "../support/fixtures";
import {
  cardOf,
  cards,
  firstPageRead,
  type JobDetailOut,
  SCORE_LABEL,
  shownPage,
  UNSCORED_CHIP,
  wholeList,
} from "../support/jobs";
import { readSettings } from "../support/settings";
import { jobOfPosting } from "../support/stack";

const KEYWORD = "Entwickler";
const ADZUNA_NATIVE_ID = "5929169784";
const ADZUNA_TITLE = "Java-Entwickler";
const ASHBY_TITLE = "Sr Security Engineer | Remote | Europe";
const SETUP_PLACE = "Berlin";
const RADIUS_KM = "30";
const ALL_WORK_MODES = ["hybrid", "onsite", "remote", "unknown"] as const;
const WEEK_MS = 7 * 24 * 3_600_000;
const CLOCK_SLACK_MS = 60_000;

function group(page: Page, legend: string): Locator {
  return page.getByRole("complementary", { name: en["filters.title"] }).getByRole("group", { name: legend, exact: true });
}

function box(page: Page, legend: string, label: string): Locator {
  return group(page, legend).getByRole("checkbox", { name: label, exact: true });
}

async function reset(page: Page): Promise<void> {
  await page.getByRole("button", { name: en["filters.reset"], exact: true }).click();
  await expect(page.getByLabel(en["filters.search"], { exact: true })).toHaveValue("");
}

/** A job's posted date as the list filters it: `posted_at` (effective), else its first sighting. */
function postedOrSeen(job: { posted_at: string | null; first_seen_at: string }): number {
  return Date.parse(job.posted_at ?? job.first_seen_at);
}

test("E7: the filters list and drop named cards, and a reload keeps them", async ({ page }) => {
  const adzunaJob = await jobOfPosting("adzuna", ADZUNA_NATIVE_ID);
  expect(adzunaJob, `the setup's collection holds the Adzuna posting ${ADZUNA_NATIVE_ID}`).not.toBeNull();
  const adzuna = cardOf(page, adzunaJob ?? 0);
  const api = await apiContext(page);
  try {
    const berlin = (await readSettings(api)).payload.scope.locations.find((place) => place.label === SETUP_PLACE);
    expect(berlin, `the setup's scope location ${SETUP_PLACE}`).toBeDefined();
    const loc = { loc_id: berlin?.gazetteer_id ?? "", loc_lat: String(berlin?.lat), loc_lon: String(berlin?.lon) };
    const search = page.getByLabel(en["filters.search"], { exact: true });
    const place = page.getByRole("combobox", { name: en["filters.place"] });
    const radius = page.getByLabel(en["filters.radius"], { exact: true });
    const workModes = ALL_WORK_MODES.map((mode) =>
      box(page, en["filters.work_mode"], en[`filters.work_mode.${mode}` as const]),
    );
    const unknownMode = box(page, en["filters.work_mode"], en["filters.work_mode.unknown"]);

    const defaults = firstPageRead(page);
    await page.goto("/");
    await shownPage(page, defaults);
    await expectNoSeriousA11yViolations(page);

    await test.step("1: the keyword lists the Adzuna card", async () => {
      const read = firstPageRead(page, { q: KEYWORD });
      await search.fill(KEYWORD);
      const answer = await shownPage(page, read);
      expect(answer.items.map((card) => card.job_id)).toContain(adzunaJob);
      await expect(adzuna.getByRole("heading", { level: 2 })).toHaveText(ADZUNA_TITLE);
      await expectNoSeriousA11yViolations(page);
    });

    const near = { q: KEYWORD, ...loc, radius_km: RADIUS_KM };
    await test.step("2: Berlin at 30 km keeps it listed", async () => {
      const read = firstPageRead(page, near);
      await place.fill(SETUP_PLACE);
      await page.getByRole("option", { name: new RegExp(`^${SETUP_PLACE} · `) }).first().click();
      const answer = await shownPage(page, read);
      expect(answer.items.map((card) => card.job_id)).toContain(adzunaJob);
      await expect(place).toHaveValue(SETUP_PLACE);
      await expect(radius).toHaveValue(RADIUS_KM);
      await expect(adzuna).toBeVisible();
      await expectNoSeriousA11yViolations(page);
    });

    await test.step("3: work mode unknown unticked drops it, ticked again lists it", async () => {
      const read = firstPageRead(page, { ...near, work_mode: ["hybrid", "onsite", "remote"] });
      await unknownMode.uncheck();
      const answer = await shownPage(page, read);
      expect(answer.items.map((card) => card.job_id)).not.toContain(adzunaJob);
      await expect(adzuna).toHaveCount(0);
      await unknownMode.check(); // check 2's list, from the query cache
      await expect(adzuna).toBeVisible();
      await expectNoSeriousA11yViolations(page);
    });

    await test.step("7: a reload of check 3's state keeps the URL and the controls", async () => {
      const url = page.url();
      const read = firstPageRead(page, near);
      await page.reload();
      await shownPage(page, read);
      expect(page.url()).toBe(url);
      await expect(search).toHaveValue(KEYWORD);
      await expect(place).toHaveValue(SETUP_PLACE);
      await expect(radius).toHaveValue(RADIUS_KM);
      for (const mode of workModes) {
        await expect(mode).toBeChecked();
      }
      await expect(page.getByRole("checkbox", { name: en["filters.include_remote"], exact: true })).toBeChecked();
      await expect(adzuna).toBeVisible();
      await expectNoSeriousA11yViolations(page);
    });

    await test.step("4: remote alone lists the Ashby n8n card, every job known remote", async () => {
      await reset(page);
      const read = firstPageRead(page, { work_mode: "remote" });
      for (const mode of ["hybrid", "onsite", "unknown"] as const) {
        await box(page, en["filters.work_mode"], en[`filters.work_mode.${mode}`]).uncheck();
      }
      const answer = await shownPage(page, read);
      const listed = await wholeList(api, await read, answer);
      const ashby = listed.filter((card) => card.title === ASHBY_TITLE && card.source_ids.includes("ashby"));
      expect(ashby.length, `the Ashby n8n card "${ASHBY_TITLE}" among ${String(listed.length)} remote cards`).toBe(1);
      const unknown = listed.filter((card) => !card.work_mode_known || !card.work_modes.includes("remote"));
      expect(unknown.map((card) => card.job_id), "listed jobs without a known remote mode").toEqual([]);
      await expect(page.getByRole("heading", { level: 2, name: ASHBY_TITLE, exact: true })).toBeVisible();
      await expectNoSeriousA11yViolations(page);
    });

    await test.step("5: posted within 7 days", async () => {
      await reset(page);
      const read = firstPageRead(page, { posted: "7d" });
      await group(page, en["filters.posted"]).getByRole("radio", { name: en["filters.posted.7d"], exact: true }).check();
      const answer = await shownPage(page, read);
      const listed = await wholeList(api, await read, answer);
      expect(listed.length, "jobs posted within 7 days").toBeGreaterThan(0);
      const since = Date.now() - WEEK_MS - CLOCK_SLACK_MS;
      const older: string[] = [];
      for (const card of listed.filter((c) => postedOrSeen(c) < since)) {
        // a group card is listed when its representative or one of its deferred members matches (§8.7)
        const members = await Promise.all(
          (card.group?.members ?? []).map((member) => getJson<JobDetailOut>(api, `/api/jobs/${String(member.job_id)}`)),
        );
        if (!members.some((member) => postedOrSeen(member.job) >= since)) {
          older.push(`${String(card.job_id)} ${card.posted_at ?? card.first_seen_at}`);
        }
      }
      expect(older, "listed cards posted (or first seen) more than 7 days ago, none of their group's either").toEqual([]);
      await expectNoSeriousA11yViolations(page);
    });

    await test.step("6: contested only, then unscored / pending alone", async () => {
      await reset(page);
      const contested = firstPageRead(page, { contested: "only" });
      await group(page, en["filters.contested"]).getByRole("radio", { name: en["filters.tri.only"], exact: true }).check();
      const shown = await shownPage(page, contested);
      expect(shown.items.length, "contested cards").toBeGreaterThan(0);
      for (const card of await cards(page).all()) {
        await expect(card.getByRole("list", { name: en["card.badges"] }).getByText(en["badge.contested"], { exact: true })).toBeVisible();
      }
      await expectNoSeriousA11yViolations(page);

      await reset(page);
      const unscored = firstPageRead(page, { scoring_state: "unscored" });
      for (const state of ["scored", "filtered", "failed", "expired"] as const) {
        await box(page, en["filters.scoring_state"], en[`filters.scoring_state.${state}`]).uncheck();
      }
      const answer = await shownPage(page, unscored);
      expect(answer.items.length, "unscored cards").toBeGreaterThan(0);
      for (const card of await cards(page).all()) {
        await expect(card.getByText(UNSCORED_CHIP)).toBeVisible();
        await expect(card.getByRole("img", { name: SCORE_LABEL })).toHaveCount(0);
      }
      await expectNoSeriousA11yViolations(page);
    });
  } finally {
    await api.dispose();
  }
});
