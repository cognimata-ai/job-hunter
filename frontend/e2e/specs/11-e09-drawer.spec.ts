// E9 (spec §18.7, §15.4, §8.8; plan M4 Task 34, resolution d): first, the grounded job (setup step 7,
// `groundedJobId`) published under the active tuple of profile v2 (E3 made it): a target still to score is
// scored now (no bucket: score-now is outside the cycle's admission), its standing keyed answers serving every
// try of its revision; its detail is contested, with three critic rounds (the first with T1), the level diff
// 3 → 2 on the critic's criterion, and at least three criteria whose description evidence slices to its quote.
// Then its drawer: the criteria table, the evidence quotes marked in the description, the Contested tab's three
// rounds and the rejected draft's level diff, and Provenance's postings. Then the drawer of the setup's merged
// pair (setup step 5): Unmerge of its merge event, after which both jobs are listed again.
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { answerTo, type ApiContext, apiContext, getJson } from "../support/api";
import { en, expect, filled, templateMatch, test } from "../support/fixtures";
import { type JobDetailOut, type JobsPageOut, scoreNowPublished } from "../support/jobs";
import { groundedJobId, psql } from "../support/stack";

type DetailScoreOut = components["schemas"]["DetailScoreOut"];

// The grounded answers (`scripts/m4_seeds.py`): three quotes, three critic rounds, the critic's level 3 → 2.
const QUOTES = 3;
const CRITIC_ROUNDS = 3;
const FIRST_TRIGGER = "T1";
const DISPUTED_CRITERION = "role_fit";
const GENERATED_LEVEL = 3;
const DISPUTED_LEVEL = 2;
// A target score-now takes (`scoring.targets.ACCEPTED_STATES`, but a deferred member's).
const TO_SCORE: readonly string[] = ["pending", "budget_deferred", "expired_unscored"];
const OPEN_MERGE =
  "SELECT id || '|' || from_job_id || '|' || into_job_id FROM merge_events " +
  "WHERE kind = 'user_confirmed_merge' AND reversed_by_event_id IS NULL ORDER BY id LIMIT 1";

// Unmerge is one-shot per stack: a retry could only fail on the reversed merge (Ruling T33-b).
test.describe.configure({ retries: 0 });

/** `text[start:end]` in code points, as the api's spans count (`jobs/codepoints.ts`). */
function slice(text: string, start: number, end: number): string {
  return Array.from(text).slice(start, end).join("");
}

/** The description quotes whose spans slice to them, one per criterion that has one. */
function spannedQuotes(detail: JobDetailOut, score: DetailScoreOut): string[] {
  const text = detail.description.text;
  return score.criteria.flatMap((criterion) => {
    const found = criterion.evidence.find(
      (e) =>
        e.source_kind === "description" &&
        e.span_start !== null &&
        e.span_end !== null &&
        slice(text, e.span_start, e.span_end) === e.quote,
    );
    return found === undefined ? [] : [found.quote];
  });
}

/** What the grounded result lacks (`e2e_setup.grounded_problem`'s predicate), or `null`. */
function groundedProblem(detail: JobDetailOut): string | null {
  const score = detail.score;
  if (score?.result_status !== "contested") {
    return `result_status=${String(score?.result_status)}`;
  }
  const spanned = spannedQuotes(detail, score).length;
  if (spanned < QUOTES) {
    return `${String(spanned)} criteria with description spans that slice to their quotes, not ${String(QUOTES)}`;
  }
  const rounds = detail.contested?.rounds ?? [];
  if (rounds.length !== CRITIC_ROUNDS) {
    return `critic rounds=${String(rounds.length)}`;
  }
  if (!(rounds.at(0)?.triggers ?? []).includes(FIRST_TRIGGER)) {
    return `the first round's triggers=${JSON.stringify(rounds.at(0)?.triggers)}`;
  }
  const diffs = (detail.contested?.rejected_drafts ?? []).flatMap((draft) => draft.level_diff);
  const wanted = diffs.some(
    (d) => d.criterion === DISPUTED_CRITERION && d.from_level === GENERATED_LEVEL && d.to_level === DISPUTED_LEVEL,
  );
  return wanted ? null : `no rejected draft with ${DISPUTED_CRITERION} ${String(GENERATED_LEVEL)} → ${String(DISPUTED_LEVEL)}`;
}

function detailOf(api: ApiContext, jobId: number): Promise<JobDetailOut> {
  return getJson<JobDetailOut>(api, `/api/jobs/${String(jobId)}`);
}

/** The jobs of the whole default list (what the Jobs page lists), through the API. */
async function listedJobs(api: ApiContext): Promise<Set<number>> {
  const ids = new Set<number>();
  let path: string | null = "/api/jobs?limit=100";
  while (path !== null) {
    const answer: JobsPageOut = await getJson<JobsPageOut>(api, path);
    answer.items.forEach((card) => ids.add(card.job_id));
    path = answer.next_cursor === null ? null : `/api/jobs?limit=100&cursor=${encodeURIComponent(answer.next_cursor)}`;
  }
  return ids;
}

test("E9: the grounded job's drawer under profile v2, and Unmerge of the merged pair", async ({ page }) => {
  test.setTimeout(420_000);
  const api = await apiContext(page);
  try {
    const jobId = await groundedJobId();
    const grounded = await test.step("the grounded job published under the active tuple", async () => {
      let detail = await detailOf(api, jobId);
      const state = detail.target?.state ?? "none";
      if (TO_SCORE.includes(state)) {
        detail = await scoreNowPublished(api, jobId, "setup step 7's grounded job under profile v2");
      } else {
        expect(state, `setup step 7's grounded job ${String(jobId)}: its target under the active tuple`).toMatch(/^published_/);
      }
      expect(groundedProblem(detail), `setup step 7's grounded job ${String(jobId)} under profile v2`).toBeNull();
      return detail;
    });
    const score = grounded.score as DetailScoreOut;
    const quotes = [...new Set(spannedQuotes(grounded, score))];

    await page.goto(`/?job=${String(jobId)}`);
    const drawer = page.getByRole("dialog", { name: grounded.job.title, exact: true });
    await expect(drawer).toBeVisible();
    await test.step("the criteria and the evidence marked in the description", async () => {
      await expect(drawer.getByRole("table", { name: en["drawer.criteria"] })).toBeVisible();
      const description = drawer.getByRole("tabpanel", { name: en["drawer.tab.description"] });
      await expect(description).toBeVisible();
      await expect.poll(() => description.locator("mark").allTextContents()).toEqual(expect.arrayContaining(quotes));
      await expectNoSeriousA11yViolations(page);
    });

    await test.step("the Contested tab: three rounds and the rejected draft's level diff", async () => {
      await drawer.getByRole("tab", { name: en["drawer.tab.contested"] }).click();
      const contested = drawer.getByRole("tabpanel", { name: en["drawer.tab.contested"] });
      for (let round = 1; round <= CRITIC_ROUNDS; round++) {
        await expect(contested.getByRole("heading", { name: templateMatch(en["contested.round"], { round: String(round) }) })).toBeVisible();
      }
      await expect(contested.getByRole("heading", { name: templateMatch(en["contested.round"], { round: String(CRITIC_ROUNDS + 1) }) })).toHaveCount(0);
      const first = contested.getByRole("article").filter({ has: page.getByRole("heading", { name: templateMatch(en["contested.round"], { round: "1" }) }) });
      await expect(first.getByText(en[`critic_trigger.${FIRST_TRIGGER}`])).toBeVisible();
      const diff = `${en[`criterion.${DISPUTED_CRITERION}`]}: ${String(GENERATED_LEVEL)} → ${String(DISPUTED_LEVEL)}`;
      await expect(contested.getByRole("listitem").filter({ hasText: diff }).first()).toBeVisible();
      await expectNoSeriousA11yViolations(page);
    });

    await test.step("Provenance: the postings", async () => {
      await drawer.getByRole("tab", { name: en["drawer.tab.provenance"] }).click();
      const provenance = drawer.getByRole("tabpanel", { name: en["drawer.tab.provenance"] });
      await expect(provenance.getByRole("heading", { name: en["provenance.postings"] })).toBeVisible();
      expect(grounded.postings.length).toBeGreaterThan(0);
      for (const posting of grounded.postings) {
        await expect(provenance.getByRole("cell", { name: posting.native_id, exact: true })).toBeVisible();
      }
      await expectNoSeriousA11yViolations(page);
    });
    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();

    await test.step("Unmerge of the setup's merged pair lists both jobs again", async () => {
      const open = await psql(OPEN_MERGE);
      expect(open, "an unreversed user-confirmed merge (setup step 5): E9's Unmerge needs a fresh stack").not.toBe("");
      const [eventId, from, into] = open.split("|").map(Number);
      expect([...(await listedJobs(api))].filter((id) => id === from), `merged job ${String(from)} listed`).toEqual([]);
      const survivor = await detailOf(api, into);
      await page.goto(`/?job=${String(into)}`);
      const pairDrawer = page.getByRole("dialog", { name: survivor.job.title, exact: true });
      await pairDrawer.getByRole("tab", { name: en["drawer.tab.provenance"] }).click();
      const provenance = pairDrawer.getByRole("tabpanel", { name: en["drawer.tab.provenance"] });
      const row = provenance.getByRole("row").filter({
        hasText: filled(en["provenance.merge_jobs"], { from: String(from), into: String(into) }),
      });
      await row.getByRole("button", { name: en["provenance.unmerge"], exact: true }).click();
      const confirm = page.getByRole("alertdialog", { name: templateMatch(en["provenance.unmerge_title"], { job: String(from) }) });
      await expect(confirm).toBeVisible();
      await expectNoSeriousA11yViolations(page);
      const answered = answerTo(page, "POST", `/api/jobs/${String(into)}/unmerge`);
      await confirm.getByRole("button", { name: en["provenance.unmerge"], exact: true }).click();
      const response = await answered;
      expect(response.status(), `the unmerge of merge event ${String(eventId)}`).toBe(202);
      expect(response.request().postDataJSON()).toEqual({ merge_event_id: eventId });
      await expect(page.getByText(templateMatch(en["provenance.unmerged"], { job: String(from) }))).toBeVisible();
      const listed = await listedJobs(api);
      expect([from, into].filter((id) => listed.has(id)), "both jobs of the pair listed again").toEqual([from, into]);
      await expectNoSeriousA11yViolations(page);
    });
  } finally {
    await api.dispose();
  }
});
