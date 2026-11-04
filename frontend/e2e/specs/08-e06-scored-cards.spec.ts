// E6 (spec §18.7, §6.9.2, §14.3, §15.3; plan M4 Task 33, resolution h): on v2's fresh tuple (E3), whose first
// page shows fewer than 10 scored cards, the score lane's bucket is refilled (`advanceBucket`: the setup's first
// periodic cycle overspent it) and `POST /api/system/score-cycle` queues a manual cycle; that cycle, found in
// `GET /api/score-cycles` on the active tuple, completes and at least 10 cards are scored, all within 300 s.
// Each scored card shows M, [L, U], its coverage and its confidence. Under "Score (neutral midpoint)" the
// badges' M are non-increasing, under "Score (conservative lower bound)" their L; and when the M page orders two
// scored cards one way while their L say the other (read through the API), the L page orders them the other way.
// Similarities may still be NULL (the reconciler's grace), so nothing here reads them.
import type { Locator, Page, Response } from "@playwright/test";
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { type ApiContext, apiContext, getJson } from "../support/api";
import { en, expect, templateMatch, test, textPattern } from "../support/fixtures";
import { cardJobId, SCORE_LABEL } from "../support/jobs";
import { advanceBucket, psql, waitFor } from "../support/stack";

type JobsPageOut = components["schemas"]["JobsPageOut"];
type JobCardOut = components["schemas"]["JobCardOut"];
type ScoreCycleQueuedOut = components["schemas"]["ScoreCycleQueuedOut"];
type ScoreCyclesPageOut = components["schemas"]["ScoreCyclesPageOut"];
type ScoreCycleOut = components["schemas"]["ScoreCycleOut"];
type ScoreSort = "score_mid" | "score_low";

const SCORED_AT_LEAST = 10;
const SCORED_WITHIN_MS = 300_000;
const QUIET_WITHIN_MS = 120_000;
// The seconds of the minute away from the worker's tick (`smoke_m3.AWAY_FROM_THE_TICK`): the tick's periodic
// trigger, at the top of each minute, would otherwise take the refill before the manual cycle.
const AWAY_FROM_THE_TICK = { from: 10, to: 45 } as const;
const ACTIVE_TUPLE = "SELECT llm_tuple_id FROM active_config WHERE id = 1";
const CYCLES = "/api/score-cycles?limit=20";

// One run per stack: once its cycle has scored the fresh tuple, a retry could only fail on the precondition,
// hiding the first failure's cause (Ruling T33-b).
test.describe.configure({ retries: 0 });
const SORT_LABEL: Record<ScoreSort, string> = {
  score_mid: en["jobs.sort.score_mid"],
  score_low: en["jobs.sort.score_low"],
};
const CONFIDENCE = new RegExp(`^(${[en["confidence.low"], en["confidence.medium"], en["confidence.high"]].join("|")})$`);

interface ShownCard {
  jobId: number;
  mid: number;
  low: number;
  high: number;
  /** The card (its <article>). */
  card: Locator;
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
 * bar's text label), and the card. */
async function shownCards(page: Page): Promise<ShownCard[]> {
  const cards = page.getByRole("article").filter({ has: page.getByRole("img", { name: SCORE_LABEL }) });
  const shown: ShownCard[] = [];
  for (const card of await cards.all()) {
    const bar = card.getByRole("img", { name: SCORE_LABEL });
    const [, mid, low, high] = SCORE_LABEL.exec((await bar.getAttribute("aria-label")) ?? "") ?? [];
    shown.push({
      jobId: await cardJobId(card),
      mid: Number(mid),
      low: Number(low),
      high: Number(high),
      card,
    });
  }
  return shown;
}

/** The newest cycles (`GET /api/score-cycles`, newest first). */
async function cycles(api: ApiContext): Promise<ScoreCycleOut[]> {
  return (await getJson<ScoreCyclesPageOut>(api, CYCLES)).items;
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
    // The precondition: E3's fresh tuple, whose cards no cycle has scored yet.
    const tuple = Number(await psql(ACTIVE_TUPLE));
    const scoredBefore = scored(await firstPage(api, "score_mid")).length;
    const precondition =
      `the active tuple ${String(tuple)} shows ${String(scoredBefore)} scored cards already: ` +
      "E6 needs E3's fresh tuple (profile v2), which no cycle has scored";
    expect(scoredBefore, precondition).toBeLessThan(SCORED_AT_LEAST);

    // A quiet moment away from the tick, the bucket refilled, the manual cycle requested.
    const after = await waitFor(
      async () => {
        const second = new Date().getUTCSeconds();
        const newest = (await cycles(api)).at(0);
        const away = second >= AWAY_FROM_THE_TICK.from && second <= AWAY_FROM_THE_TICK.to;
        return away && newest?.status !== "running" ? (newest?.id ?? 0) : null;
      },
      QUIET_WITHIN_MS,
      "no cycle running, at a second away from the worker's tick",
    );
    await advanceBucket();
    const requested = await api.post("/api/system/score-cycle", { headers: { "Idempotency-Key": api.key() } });
    expect(requested.status()).toBe(202);
    const queued = (await requested.json()) as ScoreCycleQueuedOut;
    expect(queued.queued, `the cycle request (reason ${String(queued.reason)})`).toBe(true);

    // The manual cycle on the active tuple completes, and its cards are scored.
    const cycle = await waitFor(
      async () => {
        const mine = (await cycles(api)).find(
          (c) => c.id > after && c.trigger === "manual" && c.llm_tuple_id === tuple,
        );
        if (mine?.status === "failed") {
          throw new Error(`manual cycle ${String(mine.id)} failed (${String(mine.status_reason)})`);
        }
        return mine?.status === "completed" ? mine : null;
      },
      SCORED_WITHIN_MS,
      `a completed manual cycle of tuple ${String(tuple)} after cycle ${String(after)} (the request's reason ${String(queued.reason)})`,
    );
    const scoredAfter = await waitFor(
      async () => {
        const count = scored(await firstPage(api, "score_mid")).length;
        return count >= SCORED_AT_LEAST ? count : null;
      },
      SCORED_WITHIN_MS,
      `${String(SCORED_AT_LEAST)} scored cards after cycle ${String(cycle.id)}`,
    );
    test.info().annotations.push({
      type: "cycle",
      description:
        `manual cycle ${String(cycle.id)} of tuple ${String(tuple)}: admitted ${JSON.stringify(cycle.admitted)}; ` +
        `scored cards ${String(scoredBefore)} -> ${String(scoredAfter)}`,
    });

    // Score (neutral midpoint), the default: each card's M, [L, U], coverage and confidence; M non-increasing.
    const byMid = await showSorted(page, "score_mid", () => page.goto("/"));
    expect(byMid.length).toBeGreaterThanOrEqual(SCORED_AT_LEAST);
    for (const { card, ...shown } of byMid) {
      await expect(card.getByText(String(shown.mid), { exact: true })).toBeVisible();
      const range = templateMatch(en["score.range"], { low: String(shown.low), high: String(shown.high) });
      await expect(card.getByText(range)).toBeVisible();
      await expect(card.getByText(textPattern(en["score.coverage"]))).toBeVisible();
      await expect(card.getByText(CONFIDENCE)).toBeVisible();
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

    // Through the API, the scored cards of the two first pages: when the M page puts `a` before `b` while
    // `a.low < b.low`, `b` strictly outranks `a` under L, so the L page lists `b` before `a`, or no longer lists
    // `a` (a page is a prefix of its sort's scored section, so `b` cannot be missing while `a` is listed).
    const [midPage, lowPage] = [await firstPage(api, "score_mid"), await firstPage(api, "score_low")];
    const ranked = scored(midPage).map((card) => ({
      id: card.job_id,
      mid: Number(card.score?.mid),
      low: Number(card.score?.low),
    }));
    const lowOrder = scored(lowPage).map((card) => card.job_id);
    const pairs = ranked.flatMap((a, i) => ranked.slice(i + 1).map((b) => [a, b] as const));
    const pair = pairs.find(([a, b]) => a.low < b.low);
    if (pair === undefined) {
      const description = `no pair of the ${String(ranked.length)} scored cards whose M and L orders disagree`;
      test.info().annotations.push({ type: "pair check", description });
      console.log(`E6: ${description}`);
    } else {
      const [a, b] = pair;
      const [atA, atB] = [lowOrder.indexOf(a.id), lowOrder.indexOf(b.id)];
      const said =
        `the M page lists job ${String(a.id)} (M ${String(a.mid)}, L ${String(a.low)}) before job ${String(b.id)} ` +
        `(M ${String(b.mid)}, L ${String(b.low)}); the L page at ${String(atB)} and ${String(atA)}`;
      expect(atA === -1 || (atB !== -1 && atB < atA), said).toBe(true);
      test.info().annotations.push({ type: "pair check", description: said });
      console.log(`E6: ${said}`);
    }
  } finally {
    await api.dispose();
  }
});
