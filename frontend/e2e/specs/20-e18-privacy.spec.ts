// E18 (spec §18.7, §15.7, §20.3-§20.6, §19.5; plan M5 Task 30): Privacy & data on the set-up stack, the last
// spec of all because it deletes everything (one worker, the files in name order). A fresh load of the privacy
// statement's address shows it and keeps it in view while the page fills in (plan M5 Task 33); Privacy & data's
// link scrolls to it, and its contents scroll to each of its sections. (a) Export → ready → the download: a
// zip (`PK\x03\x04`) whose central directory names README.txt and jobs.json. (b) Delete everything: Submit stays
// disabled until the exact phrase (a wrong one is marked), and a wrong password is refused with the
// `reauth_failed` text. (c) The phrase and the password: 202, "Deleting…" and the executing state, checked with
// the worker paused (`docker compose pause worker`, from before the request until after those checks), so a fast
// purge cannot end the session first (final review M2); it is paused only outside every transaction, so it never
// holds a lock the request waits for (final fix C8), and the cleanup unpauses it whatever ends the spec. Then the
// completion ends the session and the next read leads to the login; the same user and password sign in again (M5-U3), the Jobs page is the
// first-use empty state, and the deletion shows "Completed — not yet confirmed offsite" with the offsite banner,
// never "Confirmed offsite", until the shipper has run (`runShipper`, the shipper service of the dev stack, whose
// local restic repository the spec initialises first): then "Confirmed offsite" and no banner within 120 s. The
// CSP guard (`fixtures.ts`) runs throughout, and axe on every state the page shows.
//
// The re-authentication's failure counts as a failed login of devproxy's one address (§16.3: five within 15 min
// lock it out). E01's five failures may still be inside that window, so this sixth one may lock the address out:
// the spec then runs `jobhunter sessions unlock` (§16.3's escape hatch, as E01 does) before the request that must
// pass. Its one login stays within the edge's login budget (E01's note).
import { readFile } from "node:fs/promises";
import type { Locator, Page } from "@playwright/test";
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { answerTo, apiContext, getJson, ORIGIN } from "../support/api";
import { en, expect, fillPassword, filled, signInThroughUi, templateMatch, test } from "../support/fixtures";
import { compose, initResticRepo, pauseWorkerOutsideTransactions, runShipper, smokeUser } from "../support/stack";

type ManifestOut = components["schemas"]["ManifestOut"];
type ManifestQueuedOut = components["schemas"]["ManifestQueuedOut"];

/** The phrase the server takes (`api/routes/privacy.py`, `privacyApi.PHRASES`), typed exactly. */
const PHRASE = "DELETE EVERYTHING";
const WRONG_PASSWORD = "not-the-smoke-password-e18";
/** The export of the set-up stack's data, written by the worker. */
const EXPORT_WITHIN_MS = 120_000;
/** The deletion of the whole set-up stack, then the next background read that finds the session gone. */
const LOGGED_OUT_WITHIN_MS = 300_000;
/** The shipper's run, then the deletions' read (every 10 s) and the status's (every 60 s) that show it. */
const SHIPPED_WITHIN_MS = 120_000;
const DELETE_PATH = "/api/privacy/delete-everything";
// a zip's local file header, and the signatures of its central directory's records (APPNOTE 4.3.7, 4.3.12, 4.3.16)
const LOCAL_FILE_HEADER = [0x50, 0x4b, 0x03, 0x04];
const CENTRAL_FILE_HEADER = 0x02014b50;
const END_OF_CENTRAL_DIRECTORY = 0x06054b50;
const END_RECORD_BYTES = 22;
const MAX_COMMENT_BYTES = 0xffff;

// Destructive and one-shot per stack: a retry would find nothing left to export or delete.
test.describe.configure({ retries: 0 });

/** Whether the spec paused the worker and has not unpaused it yet (part c). */
let workerPaused = false;

/** The worker paused outside every transaction (the harness's `pauseWorkerOutsideTransactions`, final fix C8:
 * frozen inside one, it could hold a lock the request waits for): the purge it would run waits until
 * `unpauseWorker`. */
async function pauseWorker(): Promise<void> {
  await pauseWorkerOutsideTransactions(); // a pause that fails leaves the worker running
  workerPaused = true;
}

async function unpauseWorker(): Promise<void> {
  await compose(["unpause", "worker"]);
  workerPaused = false;
}

// A failure or a timeout between the pause and the unpause never leaves the stack's worker paused.
test.afterEach(async () => {
  if (workerPaused) {
    await unpauseWorker();
  }
});

/** The names of a zip's entries, read from its central directory (where the end record says it starts). */
function zipEntryNames(zip: Uint8Array): string[] {
  const view = new DataView(zip.buffer, zip.byteOffset, zip.byteLength);
  let end = -1;
  for (let at = zip.length - END_RECORD_BYTES; at >= Math.max(0, zip.length - END_RECORD_BYTES - MAX_COMMENT_BYTES); at -= 1) {
    if (view.getUint32(at, true) === END_OF_CENTRAL_DIRECTORY) {
      end = at;
      break;
    }
  }
  expect(end, "the zip's end of central directory record").toBeGreaterThanOrEqual(0);
  const entries = view.getUint16(end + 10, true);
  const names: string[] = [];
  let at = view.getUint32(end + 16, true);
  for (let entry = 0; entry < entries; entry += 1) {
    expect(view.getUint32(at, true), `the central directory's entry ${String(entry)}`).toBe(CENTRAL_FILE_HEADER);
    const nameBytes = view.getUint16(at + 28, true);
    const extraBytes = view.getUint16(at + 30, true);
    const commentBytes = view.getUint16(at + 32, true);
    names.push(new TextDecoder().decode(zip.subarray(at + 46, at + 46 + nameBytes)));
    at += 46 + nameBytes + extraBytes + commentBytes;
  }
  return names;
}

/** Privacy & data's card titled `title` (a `settings_privacy.*.title`). */
function card(page: Page, title: string): Locator {
  return page.getByRole("region", { name: en["settings.section.privacy"] }).getByRole("region", { name: title, exact: true });
}

/** The deletion `id` of kind delete-everything in Privacy & data's list. */
function deletionItem(page: Page, id: number): Locator {
  const title = filled(en["settings_privacy.deletion_title"], {
    kind: en["settings_privacy.kind.delete_everything"],
    id: String(id),
  });
  return page
    .getByRole("region", { name: en["settings_privacy.deletions"] })
    .getByRole("listitem")
    .filter({ has: page.getByText(title, { exact: true }) });
}

/** A deletion's state badge (`settings_privacy.status.*`), matched whole. */
function state(item: Locator, status: "executing" | "completed" | "shipped"): Locator {
  return item.getByText(en[`settings_privacy.status.${status}`], { exact: true });
}

test("E18: the export downloads a zip; delete everything takes the exact phrase and the password, logs out, and is confirmed offsite only after the shipper ran", async ({
  page,
}) => {
  test.setTimeout(EXPORT_WITHIN_MS + LOGGED_OUT_WITHIN_MS + SHIPPED_WITHIN_MS + 180_000);
  const user = await smokeUser();
  await initResticRepo(); // the shipper's local repository: `backup init` once for a new volume, a no-op after

  // The privacy statement: a fresh load of its address (a reload, a bookmark) shows it, and keeps it in view once
  // the parts above it have read their data, the Health panel's last group among them (plan M5 Task 33).
  await page.goto("/settings#privacy-statement");
  const statementTitle = page
    .getByRole("region", { name: en["privacy_statement.title"] })
    .getByRole("heading", { name: en["privacy_statement.title"], level: 2 });
  await expect(statementTitle).toBeInViewport();
  await expect(page.getByRole("region", { name: en["health.versions"] }).getByRole("code").first()).toBeAttached();
  await expect(statementTitle).toBeInViewport();
  // Privacy & data links to it, and its contents link to its sections.
  await page.goto("/settings#privacy");
  await expect(page.getByRole("region", { name: en["settings.section.privacy"] })).toBeVisible();
  await expectNoSeriousA11yViolations(page);
  await page.getByRole("link", { name: en["settings_privacy.statement"] }).click();
  await expect(page).toHaveURL((url) => url.pathname === "/settings" && url.hash === "#privacy-statement");
  const statement = page.getByRole("region", { name: en["privacy_statement.title"] });
  await expect(statement.getByRole("heading", { name: en["privacy_statement.title"], level: 2 })).toBeInViewport();
  const lastSection = statement.getByRole("navigation", { name: en["privacy_statement.contents"] }).getByRole("link").last();
  const anchor = new URL((await lastSection.getAttribute("href")) ?? "", ORIGIN).hash;
  expect(anchor, "the last section's anchor").toMatch(/^#privacy-statement-[\w-]+$/);
  const lastHeading = page.locator(anchor);
  await expect(lastHeading, "the statement's last section, below the viewport").not.toBeInViewport();
  await lastSection.click();
  await expect(page).toHaveURL((url) => url.hash === anchor);
  await expect(lastHeading).toBeInViewport();
  await expectNoSeriousA11yViolations(page);

  // (a) The export: ready, then its zip downloaded.
  const exporting = card(page, en["settings_privacy.export.title"]);
  await exporting.getByRole("button", { name: en["settings_privacy.export.start"] }).click();
  await expect(exporting.getByText(templateMatch(en["settings_privacy.export.ready"]))).toBeVisible({ timeout: EXPORT_WITHIN_MS });
  await expectNoSeriousA11yViolations(page);
  const downloading = page.waitForEvent("download");
  await exporting.getByRole("link", { name: en["settings_privacy.export.download"] }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toMatch(/^jobhunter-export-\d+\.zip$/);
  const zip = await readFile(await download.path());
  expect([...zip.subarray(0, LOCAL_FILE_HEADER.length)], "the zip's first local file header").toEqual(LOCAL_FILE_HEADER);
  expect(zipEntryNames(zip)).toEqual(expect.arrayContaining(["README.txt", "jobs.json"]));

  // (b) Delete everything: Submit stays disabled until the exact phrase; a wrong password is refused.
  const deleting = card(page, en["settings_privacy.delete.title"]);
  await deleting.getByRole("button", { name: en["settings_privacy.delete.open"] }).click();
  const dialog = page.getByRole("alertdialog", { name: en["settings_privacy.delete.dialog_title"] });
  const phrase = dialog.getByLabel(en["settings_privacy.phrase_label"], { exact: true });
  const submit = dialog.getByRole("button", { name: en["settings_privacy.delete.submit"], exact: true });
  await expect(dialog.getByText(PHRASE, { exact: true })).toBeVisible();
  await expect(submit).toBeDisabled();
  await fillPassword(page, WRONG_PASSWORD);
  await phrase.fill(PHRASE.toLowerCase()); // no prefix of the phrase: marked as the mismatch it is
  await expect(phrase).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.getByText(en["problem.confirmation_mismatch"], { exact: true })).toBeVisible();
  await expect(submit).toBeDisabled();
  await expectNoSeriousA11yViolations(page);
  await phrase.fill(PHRASE.slice(0, -1)); // a prefix: not marked, still not the phrase
  await expect(phrase).toHaveAttribute("aria-invalid", "false");
  await expect(submit).toBeDisabled();
  await phrase.fill(PHRASE);
  await expect(submit).toBeEnabled();
  const refusal = answerTo(page, "POST", DELETE_PATH);
  await submit.click();
  expect((await refusal).status()).toBe(403);
  await expect(dialog.getByRole("alert")).toContainText(en["problem.reauth_failed"]);
  await expectNoSeriousA11yViolations(page);
  await compose(["run", "--rm", "-T", "api", "jobhunter", "sessions", "unlock"]); // the note above

  // (c) The phrase and the password: requested, "Deleting…", then the session ends and the login follows. The
  // api admits the purge and answers 202 by itself; the worker, paused, runs it only once the executing state
  // has been checked.
  await fillPassword(page, user.password);
  await pauseWorker();
  const accepted = answerTo(page, "POST", DELETE_PATH);
  await submit.click();
  const answer = await accepted;
  expect(answer.status()).toBe(202);
  const manifestId = ((await answer.json()) as ManifestQueuedOut).manifest_id;
  await expect(dialog).toBeHidden();
  await expect(deleting.getByRole("status")).toHaveText(en["settings_privacy.requested"]);
  const item = deletionItem(page, manifestId);
  await expect(state(item, "executing")).toBeVisible();
  await expectNoSeriousA11yViolations(page, "#deletion-status");
  await unpauseWorker();
  await expect(page).toHaveURL(/\/login\?redirect=/, { timeout: LOGGED_OUT_WITHIN_MS });
  await expectNoSeriousA11yViolations(page);

  // The same user and password sign in again; the Jobs page is the first-use empty state.
  expect((await signInThroughUi(page, user)).status()).toBe(204);
  await expect(page).toHaveURL((url) => url.pathname === "/settings");
  await page.goto("/");
  await expect(page.getByRole("link", { name: en["jobs.no_profile"] })).toBeVisible();
  await expectNoSeriousA11yViolations(page);

  // Completed, not confirmed offsite, with the banner, until the shipper has run.
  await page.goto("/settings#privacy");
  const banner = page.getByText(en["banner.deletion_unshipped"], { exact: true });
  await expect(state(item, "completed")).toBeVisible();
  await expect(banner).toBeVisible();
  await expect(state(item, "shipped")).toHaveCount(0);
  const api = await apiContext(page);
  try {
    const completed = await getJson<ManifestOut>(api, `/api/privacy/manifests/${String(manifestId)}`);
    expect([completed.status, completed.shipped_at]).toEqual(["completed", null]);
  } finally {
    await api.dispose();
  }
  await expectNoSeriousA11yViolations(page);

  await runShipper();
  const shippedBy = Date.now() + SHIPPED_WITHIN_MS;
  await expect(state(item, "shipped")).toBeVisible({ timeout: SHIPPED_WITHIN_MS });
  await expect(banner).toHaveCount(0, { timeout: Math.max(shippedBy - Date.now(), 1) });
  await expectNoSeriousA11yViolations(page);
});
