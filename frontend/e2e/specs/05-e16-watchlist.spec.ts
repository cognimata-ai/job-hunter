// E16 (spec §18.7, §5.4, §15.7; plan M4 Task 33, resolution e): a careers URL pasted into the watchlist's add
// field is read by its pattern alone (`POST /api/watchlist/parse-url`, which fetches nothing and writes
// nothing): `https://jobs.eu.lever.co/<slug>` is detected as Lever, region EU, with that slug; a URL of no
// supported board shows its `watchlist_unsupported` message. Nothing is added: no [Add to the watchlist], no
// `POST /api/watchlist`, and the watchlist and the settings versions keep their counts.
import type { Page, Request } from "@playwright/test";
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { en, expect, test } from "../support/fixtures";
import { psql } from "../support/stack";

type ParsedUrlOut = components["schemas"]["ParsedUrlOut"];
type UnsupportedUrlOut = components["schemas"]["UnsupportedUrlOut"];

const SLUG = "e2e-detect-only";
const LEVER_EU_URL = `https://jobs.eu.lever.co/${SLUG}`;
const UNSUPPORTED_URL = "https://careers.example.org/jobs/backend-engineer";
const COUNTS = "SELECT (SELECT count(*) FROM watchlist_companies) || '|' || (SELECT count(*) FROM settings_versions)";

function isParse(request: Request): boolean {
  return request.method() === "POST" && new URL(request.url()).pathname === "/api/watchlist/parse-url";
}

/** The careers URL typed into the field, and the read of it that the field sends. */
async function typeUrl(page: Page, url: string): Promise<ParsedUrlOut | UnsupportedUrlOut> {
  const field = page.getByLabel(en["settings_watchlist.url"], { exact: true });
  const read = page.waitForResponse(
    (response) => isParse(response.request()) && (response.request().postDataJSON() as { url: string }).url === url,
  );
  await field.fill(url);
  const response = await read;
  expect(response.status()).toBe(200);
  return (await response.json()) as ParsedUrlOut | UnsupportedUrlOut;
}

test("E16: a pasted careers URL is detected by its pattern, and an unsupported one is named", async ({ page }) => {
  const counts = await psql(COUNTS);
  const writes: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET" && path.startsWith("/api/watchlist") && !isParse(request)) {
      writes.push(`${request.method()} ${path}`);
    }
  });
  await page.goto("/settings#watchlist");
  const section = page.getByRole("region", { name: en["settings.section.watchlist"] });
  await expect(section.getByRole("group", { name: en["settings_watchlist.by_url"] })).toBeVisible();
  await expectNoSeriousA11yViolations(page);

  expect(await typeUrl(page, LEVER_EU_URL)).toEqual({ ats: "lever", slug: SLUG, region: "eu" });
  const detected = section.getByRole("group", { name: en["settings_watchlist.detected"] });
  await expect(detected).toBeVisible();
  const terms = await detected.locator("dt").allTextContents();
  const values = await detected.locator("dd").allTextContents();
  expect(Object.fromEntries(terms.map((term, i) => [term, values[i]]))).toEqual({
    [en["settings_watchlist.ats"]]: en["source.lever"],
    [en["settings_watchlist.slug"]]: SLUG,
    [en["settings_watchlist.region"]]: en["lever_region.eu"],
  });
  await expect(detected.getByLabel(en["settings_watchlist.name"], { exact: true })).toHaveValue(SLUG);
  await expect(detected.getByRole("button", { name: en["settings_watchlist.add"] })).toBeEnabled();
  await expectNoSeriousA11yViolations(page);

  expect(await typeUrl(page, UNSUPPORTED_URL)).toEqual({ unsupported: "unsupported_pattern" });
  await expect(section.getByText(en["watchlist_unsupported.unsupported_pattern"], { exact: true })).toBeVisible();
  await expect(detected).toBeHidden();
  await expectNoSeriousA11yViolations(page);

  expect(writes, "watchlist writes").toEqual([]);
  expect(await psql(COUNTS), "watchlist companies | settings versions").toBe(counts);
});
