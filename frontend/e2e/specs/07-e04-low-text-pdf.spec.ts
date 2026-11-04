// E4 (spec §18.7, §12.7, §15.7; plan M4 Task 33, resolution g): `low-text.pdf` uploaded through the CV section
// fails `low_text` (too little text to read) with the Paste text repair; the repair opens the paste form, a
// pasted text of at least `PASTE_MIN` code points (an invented CV, no name and no contact) opens a new draft,
// whose text is approved and whose profile is extracted (`ready`); Discard (asked first) drops it, and the
// active profile stays v2 (E3's).
import type { Response } from "@playwright/test";
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { answerTo, type ApiContext, apiContext, DRAFT_WITHIN_MS, draftPath, draftWhen, getJson } from "../support/api";
import { en, expect, LOW_TEXT_PDF, templateMatch, test } from "../support/fixtures";

type DraftOut = components["schemas"]["DraftOut"];
type ProfileListOut = components["schemas"]["ProfileListOut"];

const ACTIVE_VERSION = 2; // E3 confirmed v2
const PASTE_MIN = 300; // `cv/api.ts` PASTE_MIN, the server's minimum (code points)
// An invented CV: no name, no address, no contact detail.
const PASTED = [
  "Backend-Entwicklerin mit Schwerpunkt auf Zahlungsdiensten und Datenplattformen.",
  "",
  "Berufserfahrung",
  "2019 - heute: Backend-Entwicklerin, Beispielwerk GmbH",
  "Entwicklung von Schnittstellen mit Python, FastAPI und PostgreSQL.",
  "2015 - 2019: Softwareentwicklerin, Musterbetrieb AG",
  "Aufbau von Datenstrecken mit Kafka und Apache Spark.",
  "",
  "Kenntnisse: Python, FastAPI, PostgreSQL, Kafka, Docker",
  "Sprachen: Deutsch (Muttersprache), Englisch (C1)",
].join("\n");

/** A 202's `{draft_id}`. */
async function draftCreated(response: Response): Promise<number> {
  expect(response.status()).toBe(202);
  return ((await response.json()) as { draft_id: number }).draft_id;
}

/** The draft through the API once it is `status`. */
function draftIn(api: ApiContext, id: number, status: DraftOut["status"]): Promise<DraftOut> {
  return draftWhen(api, id, status, (draft) => draft.status === status);
}

// One run per stack (it starts on E3's v2 without an open draft): a retry could only fail again on the moved
// state, hiding the first failure's cause (Ruling T33-b).
test.describe.configure({ retries: 0 });

test("E4: a low-text PDF fails low_text, its paste repair reaches ready, and Discard keeps v2", async ({ page }) => {
  test.setTimeout(3 * DRAFT_WITHIN_MS + 60_000);
  expect(Array.from(PASTED).length, "the pasted text's code points").toBeGreaterThanOrEqual(PASTE_MIN);
  const api = await apiContext(page);
  try {
    const before = await getJson<ProfileListOut>(api, "/api/profile");
    expect([before.active?.version_no, before.open_draft]).toEqual([ACTIVE_VERSION, null]);

    // The upload fails `low_text`, with the paste repair.
    await page.goto("/settings#cv");
    const section = page.getByRole("region", { name: en["settings.section.cv"] });
    await section.getByLabel(en["cv.upload.browse"], { exact: true }).setInputFiles(LOW_TEXT_PDF);
    await expect(section.getByLabel(en["cv.upload.native"], { exact: true })).not.toBeChecked();
    const uploaded = answerTo(page, "POST", "/api/profile/cv");
    await section.getByRole("button", { name: en["cv.upload.submit"], exact: true }).click();
    const pdfDraft = await draftCreated(await uploaded);
    const failure = section.getByRole("group", { name: en["cv.failure.title"] });
    await expect(failure).toBeVisible({ timeout: DRAFT_WITHIN_MS });
    await expect(failure.getByText(en["draft_failure.low_text"], { exact: true })).toBeVisible();
    const failed = await draftIn(api, pdfDraft, "failed");
    expect(failed.failure_code).toBe("low_text");
    expect(failed.repairs).toContain("paste_text");
    await expectNoSeriousA11yViolations(page);

    // The paste repair: the paste form, a text, a new draft through its text review to `ready`.
    await failure.getByRole("button", { name: en["draft_repair.paste_text"], exact: true }).click();
    const area = section.getByLabel(en["cv.paste.label"], { exact: true });
    await expect(area).toBeFocused();
    await area.fill(PASTED);
    await expect(section.getByText(templateMatch(en["cv.paste.count_other"]))).toBeVisible();
    await expectNoSeriousA11yViolations(page);
    const pasted = answerTo(page, "POST", "/api/profile/cv-text");
    await section.getByRole("button", { name: en["cv.paste.submit"], exact: true }).click();
    const pasteDraft = await draftCreated(await pasted);
    expect(pasteDraft).toBeGreaterThan(pdfDraft);
    const textReview = section.getByRole("region", { name: en["cv.text.title"] });
    await expect(textReview).toBeVisible({ timeout: DRAFT_WITHIN_MS });
    await draftIn(api, pasteDraft, "text_review");
    const approved = answerTo(page, "POST", draftPath(pasteDraft, "/text-review"));
    await textReview.getByRole("button", { name: templateMatch(en["cv.text.approve"]) }).click();
    expect((await approved).status()).toBe(202);
    const profileReview = section.getByRole("region", { name: en["cv.profile.title"] });
    await expect(profileReview).toBeVisible({ timeout: DRAFT_WITHIN_MS });
    expect((await draftIn(api, pasteDraft, "ready")).draft).not.toBeNull();
    await expectNoSeriousA11yViolations(page);

    // Discard, asked first: the draft is gone, v2 stays active.
    await profileReview.getByRole("button", { name: en["cv.profile.discard"], exact: true }).click();
    const ask = page.getByRole("alertdialog", { name: en["cv.discard.title"] });
    await expect(ask.getByText(en["cv.discard.body"], { exact: true })).toBeVisible();
    await expectNoSeriousA11yViolations(page);
    const discarded = answerTo(page, "POST", draftPath(pasteDraft, "/discard"));
    await ask.getByRole("button", { name: en["cv.discard.confirm"], exact: true }).click();
    expect((await discarded).status()).toBe(204);
    await expect(ask).toBeHidden();
    await expect(profileReview).toBeHidden();
    await expect(section.getByText(templateMatch(en["cv.active"], { version: String(ACTIVE_VERSION) }))).toBeVisible();
    await expectNoSeriousA11yViolations(page);
    const after = await getJson<ProfileListOut>(api, "/api/profile");
    expect([after.active?.id, after.active?.version_no, after.open_draft]).toEqual([before.active?.id, ACTIVE_VERSION, null]);
    expect((await getJson<DraftOut>(api, draftPath(pasteDraft))).status).toBe("discarded");
  } finally {
    await api.dispose();
  }
});
