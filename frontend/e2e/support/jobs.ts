// The Jobs page from a spec (plan M4 Task 34): its cards, the list's answers as the SPA reads them, a search by
// title, the state chips of an unscored card, a pending job scored now through the API, and the labelling
// queue's labelled jobs. Every text is the app's own (`en`).
import type { Locator, Page, Response } from "@playwright/test";
import type { components } from "../../src/api/schema";
import { type ApiContext, getJson, ORIGIN } from "./api";
import { en, expect, templateMatch } from "./fixtures";
import { psql, waitFor } from "./stack";

export type JobsPageOut = components["schemas"]["JobsPageOut"];
export type JobCardOut = components["schemas"]["JobCardOut"];
export type JobDetailOut = components["schemas"]["JobDetailOut"];

/** How long a score-now's target may take to be published (the setup's `PUBLISH_WITHIN_S`). */
export const PUBLISH_WITHIN_MS = 180_000;
/** The list's page size in the SPA (`useJobsList`'s `PAGE_LIMIT`). */
export const PAGE_LIMIT = 50;

/** The score badge's image name ("Score 82, range 74 to 90"), M, L and U captured. */
export const SCORE_LABEL = templateMatch(en["score.label"], { mid: /(\d+)/, low: /(\d+)/, high: /(\d+)/ });

/** The chip of a card in the `unscored` bucket (`db/ranking.py`: no target, or `pending`, `budget_deferred`,
 * `in_progress`, `in_batch`), as `StateChip` names each. */
export const UNSCORED_CHIP = new RegExp(
  [en["chip.not_queued"], en["chip.pending"], en["chip.budget_deferred"], en["chip.in_progress"], en["chip.in_batch"]]
    .map((text) => templateMatch(text).source)
    .join("|"),
);

/** A card's Score now button ("Score now", "Score now ~$0.24", "Score now <$0.01"). */
export const SCORE_NOW = new RegExp(
  [en["card.score_now"], en["card.score_now_cost"], en["card.score_now_below"]]
    .map((text) => templateMatch(text, { cost: /\S+/ }).source)
    .join("|"),
);

/** The cards of the list, in order (each an <article> named by its title). */
export function cards(page: Page): Locator {
  return page.getByRole("list", { name: en["jobs.title"] }).getByRole("article");
}

/** The card of job `jobId`: its title links to `job=<id>`, the URL's last search parameter (`JobCard`'s
 * `href`, the page's search with `job` added). */
export function cardOf(page: Page, jobId: number): Locator {
  return page.locator("article").filter({ has: page.locator(`h2 > a[href$="job=${String(jobId)}"]`) });
}

/** The job of a card: its title link's `job`. */
export async function cardJobId(card: Locator): Promise<number> {
  const href = await card.getByRole("heading", { level: 2 }).getByRole("link").getAttribute("href");
  return Number(new URL(href ?? "", ORIGIN).searchParams.get("job"));
}

/** The jobs of the cards shown, in order. */
export async function shownJobIds(page: Page): Promise<number[]> {
  const ids: number[] = [];
  for (const card of await cards(page).all()) {
    ids.push(await cardJobId(card));
  }
  return ids;
}

/** The list's filters of a request's query: every parameter but `limit` and `cursor`, sorted. */
export function filtersOf(params: URLSearchParams): string {
  const kept = new URLSearchParams([...params].filter(([name]) => name !== "limit" && name !== "cursor"));
  kept.sort();
  return kept.toString();
}

/** The SPA's next read of the list's first page (no `cursor`) under exactly the filters `filters` (each set as
 * its values; none: the defaults): start waiting before the action that sends it. A list read before under the
 * same filters is the query cache's, and is not read again. */
export function firstPageRead(page: Page, filters: Readonly<Record<string, string | readonly string[]>> = {}): Promise<Response> {
  const wanted = new URLSearchParams();
  for (const [name, value] of Object.entries(filters)) {
    for (const item of typeof value === "string" ? [value] : value) {
      wanted.append(name, item);
    }
  }
  const expected = filtersOf(wanted);
  return page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      response.request().method() === "GET" &&
      url.pathname === "/api/jobs" &&
      !url.searchParams.has("cursor") &&
      filtersOf(url.searchParams) === expected
    );
  });
}

/** The first page `read` answered (200), once the page shows its cards in its order. */
export async function shownPage(page: Page, read: Promise<Response>): Promise<JobsPageOut> {
  const response = await read;
  if (response.status() !== 200) {
    throw new Error(`GET ${new URL(response.url()).search} answered ${String(response.status())}, not 200`);
  }
  const answer = (await response.json()) as JobsPageOut;
  const ids = answer.items.map((card) => card.job_id);
  if (ids.length === 0) {
    await expect(page.getByText(en["jobs.no_match"], { exact: true })).toBeVisible();
  } else {
    await expect.poll(() => shownJobIds(page), { message: "the cards shown are the answer's, in its order" }).toEqual(ids);
  }
  return answer;
}

/** The jobs of `jobIds` that have a label: a golden case of their current revision and the active profile, as
 * the labelling queue counts one (`jobs/labelling.py`). */
export async function labelledJobs(jobIds: readonly number[]): Promise<number[]> {
  if (jobIds.length === 0) {
    return [];
  }
  const ids = jobIds.map((id) => String(Math.trunc(id))).join(",");
  const found = await psql(
    "SELECT coalesce(string_agg(j.id::text, ',' ORDER BY j.id), '') FROM jobs j " +
      "JOIN active_config a ON a.id = 1 " +
      `WHERE j.id IN (${ids}) AND EXISTS (SELECT 1 FROM golden_cases g ` +
      "WHERE g.job_revision_id = j.current_revision_id AND g.profile_version_id = a.profile_version_id)",
  );
  return found === "" ? [] : found.split(",").map(Number);
}

/** The cards of every page of the list under the filters of `first` (a first page's answer to `read`), read on
 * through the API from its `next_cursor`. */
export async function wholeList(api: ApiContext, read: Response, first: JobsPageOut): Promise<JobCardOut[]> {
  const filters = filtersOf(new URL(read.url()).searchParams);
  const items = [...first.items];
  let cursor = first.next_cursor;
  while (cursor !== null) {
    const next = new URLSearchParams(filters);
    next.set("cursor", cursor);
    next.set("limit", "100");
    const answer = await getJson<JobsPageOut>(api, `/api/jobs?${next.toString()}`);
    items.push(...answer.items);
    cursor = answer.next_cursor;
  }
  return items;
}

/** Types `title` into the filter panel's Search (every other filter at its default): the first page of the list
 * under that keyword, once shown. */
export async function searchFor(page: Page, title: string): Promise<JobsPageOut> {
  const read = firstPageRead(page, { q: title.trim() });
  await page.getByLabel(en["filters.search"], { exact: true }).fill(title);
  return shownPage(page, read);
}

/** A job of the list whose own card is `pending` (no group card: its member waits on it): the first in the list's
 * `unscored` section. */
export async function pendingJob(api: ApiContext): Promise<JobCardOut> {
  const page = await getJson<JobsPageOut>(api, "/api/jobs?scoring_state=unscored&limit=100");
  const found = page.items.find((card) => card.kind === "job" && card.target?.state === "pending");
  if (found === undefined) {
    throw new Error(`no pending job among the first ${String(page.items.length)} unscored cards`);
  }
  return found;
}

/** Job `jobId`'s detail once its active-tuple target is published (`published_*`), within `timeoutMs`; a
 * target that fails or is refused meanwhile throws with its state. */
export async function publishedDetail(api: ApiContext, jobId: number, what: string, timeoutMs = PUBLISH_WITHIN_MS): Promise<JobDetailOut> {
  return waitFor(
    async () => {
      const detail = await getJson<JobDetailOut>(api, `/api/jobs/${String(jobId)}`);
      const state = detail.target?.state ?? "none";
      if (state.startsWith("published_")) {
        return detail;
      }
      if (state === "refused" || state === "cancelled" || state.startsWith("failed_")) {
        throw new Error(`${what}: job ${String(jobId)}'s target ended ${state} (${String(detail.target?.state_reason)})`);
      }
      return null;
    },
    timeoutMs,
    `${what}: job ${String(jobId)}'s target published`,
  );
}

/** `POST /api/jobs/{id}/score-now` through the API with a fresh key (202), then the job's detail once its target
 * is published. */
export async function scoreNowPublished(api: ApiContext, jobId: number, what: string): Promise<JobDetailOut> {
  const response = await api.post(`/api/jobs/${String(jobId)}/score-now`, {
    data: {},
    headers: { "Idempotency-Key": api.key() },
  });
  if (response.status() !== 202) {
    const code = response.status() >= 400 ? ((await response.json()) as { code?: string }).code : undefined;
    throw new Error(`${what}: score-now of job ${String(jobId)} answered ${String(response.status())} (${String(code)}), not 202`);
  }
  return publishedDetail(api, jobId, what);
}
