// E6 (spec §18.7, §6.9.2, §14.3, §15.3; plan M4 Task 33, resolution h): with the score lane's bucket refilled
// (`advanceBucket`: the setup's first periodic cycle overspent it), `POST /api/system/score-cycle` queues a cycle
// on v2's tuple (E3) and at least 10 cards are scored within 300 s. Each scored card shows M, [L, U], its
// coverage and its confidence. Under "Score (neutral midpoint)" the badges' M are non-increasing, under "Score
// (conservative lower bound)" their L; and when the list holds a pair whose M and L orders disagree (read
// through the API), the two first pages differ. Similarities may still be NULL (the reconciler's grace), so
// nothing here reads them.
import type { Locator, Page, Response } from "@playwright/test";
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { type ApiContext, apiContext, getJson, ORIGIN } from "../support/api";
import { en, expect, templateMatch, test, textPattern } from "../support/fixtures";
import { advanceBucket, waitFor } from "../support/stack";

type JobsPageOut = components["schemas"]["JobsPageOut"];
type JobCardOut = components["schemas"]["JobCardOut"];
type ScoreCycleQueuedOut = components["schemas"]["ScoreCycleQueuedOut"];
type ScoreSort = "score_mid" | "score_low";

const SCORED_AT_LEAST = 10;
const SCORED_WITHIN_MS = 300_000;
const SORT_LABEL: Record<ScoreSort, string> = {
  score_mid: en["jobs.sort.score_mid"],
  score_low: en["jobs.sort.score_low"],
};
// The badge's image: "Score 82, range 74 to 90".
const SCORE_LABEL = templateMatch(en["score.label"], { mid: /(\d+)/, low: /(\d+)/, high: /(\d+)/ });
const CONFIDENCE = new RegExp(`^(${[en["confidence.low"], en["confidence.medium"], en["confidence.high"]].join("|")})$`);

interface ShownCard {
  jobId: number;
  mid: number;
  low: number;
  high: number;
  /** The card's score badge. */
  badge: Locator;
}

/** A first page of the list through the API (the SPA's page size), in `sort`. */
function firstPage(api: ApiContext, sort: ScoreSort): Promise<JobsPageOut> {
  return getJson<JobsPageOut>(api, `/api/jobs?sort=${sort}&limit=50`);
}

function scored(page: JobsPageOut): JobCardOut[] {
  return page.items.filter((card) => card.score !== null);
}

/** The list's first page as the SPA reads it in `sort` (the default sort sends none). */
function listRead(page: Page, sort: ScoreSort): Promise<Response> {
  return page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      response.request().method() === "GET" &&
      url.pathname === "/api/jobs" &&
      (url.searchParams.get("sort") ?? "score_mid") === sort &&
      !url.searchParams.has("cursor")
    );
  });
}

/** The scored cards shown, in order: each card's job (its title link's `job`), its badge's M, L and U (from the
 * bar's text label), and the badge. */
async function shownCards(page: Page): Promise<ShownCard[]> {
  const cards = page.getByRole("article").filter({ has: page.getByRole("img", { name: SCORE_LABEL }) });
  const shown: ShownCard[] = [];
  for (const card of await cards.all()) {
    const href = await card.getByRole("heading", { level: 2 }).getByRole("link").getAttribute("href");
    const bar = card.getByRole("img", { name: SCORE_LABEL });
    const [, mid, low, high] = SCORE_LABEL.exec((await bar.getAttribute("aria-label")) ?? "") ?? [];
    shown.push({
      jobId: Number(new URL(href ?? "", ORIGIN).searchParams.get("job")),
      mid: Number(mid),
      low: Number(low),
      high: Number(high),
      badge: bar.locator("xpath=../.."), // ScoreBadge: the bar's interval box, then the badge
    });
  }
  return shown;
}

/** Whether `values` never increase. */
function nonIncreasing(values: readonly number[]): boolean {
  return values.every((value, i) => i === 0 || value <= values[i - 1]);
}

/** The list in `sort` (chosen by its label, unless it is the page's sort already) once the page shows the
 * scored cards of the answer it read, in its order. */
async function showSorted(page: Page, sort: ScoreSort, choose: () => Promise<unknown>): Promise<ShownCard[]> {
  const read = listRead(page, sort);
  await choose();
  const answer = (await (await read).json()) as JobsPageOut;
  const ids = scored(answer).map((card) => card.job_id);
  await expect.poll(async () => (await shownCards(page)).map((card) => card.jobId)).toEqual(ids);
  const select = page.getByLabel(en["jobs.sort"], { exact: true });
  await expect(select).toHaveValue(sort);
  await expect(select.locator("option:checked")).toHaveText(SORT_LABEL[sort]);
  return shownCards(page);
}

test("E6: a score cycle gives scored cards, ordered by M and by L", async ({ page }) => {
  test.setTimeout(SCORED_WITHIN_MS + 180_000);
  const api = await apiContext(page);
  try {
    await advanceBucket();
    const requested = await api.post("/api/system/score-cycle", { headers: { "Idempotency-Key": api.key() } });
    expect(requested.status()).toBe(202);
    const queued = (await requested.json()) as ScoreCycleQueuedOut;
    expect(queued.queued, `the cycle request (reason ${String(queued.reason)})`).toBe(true);
    await waitFor(
      async () => {
        const count = scored(await firstPage(api, "score_mid")).length;
        return count >= SCORED_AT_LEAST ? count : null;
      },
      SCORED_WITHIN_MS,
      `${String(SCORED_AT_LEAST)} scored cards (the request's reason ${String(queued.reason)})`,
    );

    // Score (neutral midpoint), the default: each card's M, [L, U], coverage and confidence; M non-increasing.
    const byMid = await showSorted(page, "score_mid", () => page.goto("/"));
    expect(byMid.length).toBeGreaterThanOrEqual(SCORED_AT_LEAST);
    for (const { badge, ...shown } of byMid) {
      await expect(badge.getByText(String(shown.mid), { exact: true })).toBeVisible();
      const range = templateMatch(en["score.range"], { low: String(shown.low), high: String(shown.high) });
      await expect(badge.getByText(range)).toBeVisible();
      await expect(badge.getByText(textPattern(en["score.coverage"]))).toBeVisible();
      await expect(badge.getByText(CONFIDENCE)).toBeVisible();
      expect(shown.low <= shown.mid && shown.mid <= shown.high, `job ${String(shown.jobId)}: L ≤ M ≤ U`).toBe(true);
    }
    const mids = byMid.map((card) => card.mid);
    expect(nonIncreasing(mids), `M in order: ${mids.join(" ")}`).toBe(true);
    await expectNoSeriousA11yViolations(page);

    // Score (conservative lower bound): L non-increasing.
    const sortSelect = page.getByLabel(en["jobs.sort"], { exact: true });
    const byLow = await showSorted(page, "score_low", () => sortSelect.selectOption({ label: SORT_LABEL.score_low }));
    expect(byLow.length).toBeGreaterThanOrEqual(SCORED_AT_LEAST);
    const lows = byLow.map((card) => card.low);
    expect(nonIncreasing(lows), `L in order: ${lows.join(" ")}`).toBe(true);
    await expectNoSeriousA11yViolations(page);

    // Through the API: a pair whose M and L orders disagree makes the two first pages differ.
    const [midPage, lowPage] = [await firstPage(api, "score_mid"), await firstPage(api, "score_low")];
    const ranked = scored(midPage).map((card) => ({
      id: card.job_id,
      mid: Number(card.score?.mid),
      low: Number(card.score?.low),
    }));
    const pairs = ranked.flatMap((a, i) => ranked.slice(i + 1).map((b) => [a, b] as const));
    const pair = pairs.find(([a, b]) => a.mid > b.mid && a.low < b.low);
    const order = (listed: JobsPageOut) => listed.items.map((card) => card.job_id);
    if (pair === undefined) {
      const differ = JSON.stringify(order(lowPage)) === JSON.stringify(order(midPage)) ? "are equal" : "differ";
      const description = `no pair of the ${String(ranked.length)} scored cards whose M and L orders disagree (the first pages ${differ})`;
      test.info().annotations.push({ type: "pair check", description });
      console.log(`E6: ${description}`);
    } else {
      const [a, b] = pair;
      const said = `jobs ${String(a.id)} and ${String(b.id)}: M ${String(a.mid)} > ${String(b.mid)}, L ${String(a.low)} < ${String(b.low)}`;
      expect(order(lowPage), `the first pages, by ${said}`).not.toEqual(order(midPage));
      test.info().annotations.push({ type: "pair check", description: said });
    }
  } finally {
    await api.dispose();
  }
});
