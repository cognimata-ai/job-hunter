// E8 (spec §18.7, §14.3, §15.3; plan M4 Task 34): with more than one page of cards listed, page 1 is shown;
// then a publish happens (`POST /api/jobs/{id}/score-now` on a pending job through the API, its target awaited
// to `published_*`), which moves the ranking on; Load more brings page 2, whose cursor's ranking has passed, so
// "⟳ Ranking changed — refresh" shows; Refresh reads page 1 again, without the banner.
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { apiContext } from "../support/api";
import { en, expect, test } from "../support/fixtures";
import { cards, firstPageRead, type JobsPageOut, PAGE_LIMIT, pendingJob, scoreNowPublished, shownPage } from "../support/jobs";

// The publish must land between page 1 and page 2: a retry would only re-time that race (Ruling T33-b).
test.describe.configure({ retries: 0 });

test("E8: a publish between two pages raises Ranking changed, and Refresh starts again from page 1", async ({ page }) => {
  const api = await apiContext(page);
  try {
    const first = firstPageRead(page);
    await page.goto("/");
    const page1 = await shownPage(page, first);
    expect(page1.counts?.total_estimate ?? 0, "cards listed under the default filters").toBeGreaterThan(PAGE_LIMIT);
    expect(page1.next_cursor, "page 1 has a next page").not.toBeNull();
    expect(page1.ranking_changed).toBe(false);
    await expect(cards(page)).toHaveCount(PAGE_LIMIT);
    const ranking = page.getByRole("status", { name: en["jobs.ranking_status"] });
    const banner = ranking.getByRole("button", { name: en["jobs.ranking_changed"], exact: true });
    await expect(banner).toHaveCount(0);
    await expectNoSeriousA11yViolations(page);

    const pending = await pendingJob(api);
    const published = await scoreNowPublished(api, pending.job_id, "E8's publish");
    test.info().annotations.push({
      type: "publish",
      description: `job ${String(pending.job_id)}: ${String(published.target?.state)}`,
    });

    const more = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === "GET" && url.pathname === "/api/jobs" && url.searchParams.has("cursor");
    });
    await page.getByRole("button", { name: en["jobs.load_more"], exact: true }).click();
    const response = await more;
    expect(response.status()).toBe(200);
    const page2 = (await response.json()) as JobsPageOut;
    expect(page2.ranking_changed, "page 2 says the ranking moved since page 1").toBe(true);
    await expect(banner).toBeVisible();
    await expect.poll(() => cards(page).count()).toBeGreaterThan(PAGE_LIMIT);
    await expectNoSeriousA11yViolations(page);

    const again = firstPageRead(page);
    await banner.click();
    const refreshed = await shownPage(page, again);
    expect(refreshed.ranking_changed).toBe(false);
    await expect(banner).toHaveCount(0);
    await expect(cards(page)).toHaveCount(PAGE_LIMIT);
    await expectNoSeriousA11yViolations(page);
  } finally {
    await api.dispose();
  }
});
