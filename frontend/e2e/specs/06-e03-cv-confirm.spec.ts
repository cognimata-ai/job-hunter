// E3 (spec §18.7, §12.3–§12.6, §15.7; plan M4 Task 33, resolution f): `cv.docx` uploaded through the CV
// section; its strip report counts the contacts the stripper removed, and the planted phone (`PLANTED_PHONE`,
// below the stripper's minimum) is still in the text. In the text review that missed span is marked as contact
// and the text approved, so the profile is extracted (`ready`). The text is edited again from `ready` (the
// removal restored, which reopens the review and discards the extracted profile, §12.3:
// `api/routes/profile_review.py`), the phone marked again and the text approved again (`ready`). Then a skill
// other than the first and a language level are edited (`skills[0].name` stays: the setup's grounded answers
// quote it, and E9 checks the job's republication under v2), the draft saved, and Confirm opens the modal: its
// one sentence names v2 (the setup confirmed v1) with the bulk budget and the split, all online
// (`batch_bulk_rescore` is off). Confirm & re-score activates v2 and a new tuple, whose targets the API lists
// as the cards' targets, pending, while no card shows a result of the old tuple.
import type { Locator, Page } from "@playwright/test";
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { answerTo, type ApiContext, apiContext, DRAFT_WITHIN_MS, draftPath, draftWhen, getJson } from "../support/api";
import { CV_DOCX, en, expect, PLANTED_PHONE, templateMatch, test } from "../support/fixtures";
import { psql, waitFor } from "../support/stack";

type DraftOut = components["schemas"]["DraftOut"];
type ProfileListOut = components["schemas"]["ProfileListOut"];
type RescoreEstimateOut = components["schemas"]["RescoreEstimateOut"];
type ConfirmOut = components["schemas"]["ConfirmOut"];
type JobsPageOut = components["schemas"]["JobsPageOut"];
type SpendOut = components["schemas"]["SpendOut"];
type Skill = components["schemas"]["Skill"];
type Language = components["schemas"]["Language"];
type Category = NonNullable<components["schemas"]["SegmentOut"]["category"]>;

const NEXT_VERSION = 2; // the setup confirmed v1
const TEXT_WITHIN_MS = 90_000; // the text phase (docparse) and the stripping
const TARGETS_WITHIN_MS = 120_000; // the new tuple's `create_targets_for_tuple`
// The strip report's order (TextReview's `REPORT_ORDER`).
const REPORT_ORDER: readonly Category[] = ["name", "email", "phone", "address", "url", "personal"];
const SKILL_LEVELS: readonly Skill["level"][] = ["basic", "intermediate", "advanced", "expert"];
const CEFR_LEVELS: readonly Language["cefr"][] = ["A1", "A2", "B1", "B2", "C1", "C2"];
const ACTIVE_TUPLE = "SELECT llm_tuple_id FROM active_config WHERE id = 1";
// The newest `create_targets_for_tuple` of tuple {tuple} (`smoke_m3.TARGETS_CREATED`'s first column) and its
// targets.
const TARGETS_OF = (tuple: number): string =>
  "SELECT coalesce((SELECT status FROM operations WHERE kind = 'create_targets_for_tuple' " +
  `AND (payload->>'llm_tuple_id')::bigint = ${String(tuple)} ORDER BY id DESC LIMIT 1), 'none') || '|' || ` +
  `(SELECT count(*) FROM score_targets WHERE llm_tuple_id = ${String(tuple)})`;
const LAST_TARGET_OF = (tuple: number): string =>
  `SELECT coalesce(max(id), 0) FROM score_targets WHERE llm_tuple_id = ${String(tuple)}`;

// One run per stack (v1 → v2): a retry could only fail again on the moved state, hiding the first failure's cause
// (Ruling T33-b).
test.describe.configure({ retries: 0 });

/** "Removed: 1 name · 1 e-mail", as the strip report says the draft's counts. */
function stripReportText(report: DraftOut["strip_report"]): string {
  const parts = REPORT_ORDER.filter((category) => (report?.[category] ?? 0) > 0).map((category) => {
    const count = report?.[category] ?? 0;
    const template = en[`contact_category.${category}_${count === 1 ? "one" : "other"}` as const];
    return template.replace("{{count}}", String(count));
  });
  return en["cv.text.removed"].replace("{{list}}", parts.join(" · "));
}

/** `text` selected in the review's preview: the one text run of the preview that holds it. */
async function selectInPreview(review: Locator, text: string): Promise<void> {
  await review.getByTestId("cv-text").evaluate((container, wanted) => {
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
      const at = (node.textContent ?? "").indexOf(wanted);
      if (at >= 0 && node.parentElement?.closest("[data-preview]") != null) {
        const range = document.createRange();
        range.setStart(node, at);
        range.setEnd(node, at + wanted.length);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
        return;
      }
    }
    throw new Error("the wanted text is in no single run of the preview");
  }, text);
}

/** The planted phone marked as contact in `review`: the complete edit set sent (200), the phone gone from the
 * stored text and the removal there as the user's. */
async function markThePhone(page: Page, api: ApiContext, review: Locator, draftId: number): Promise<DraftOut> {
  await selectInPreview(review, PLANTED_PHONE);
  const mark = review.getByRole("button", { name: en["cv.text.mark"], exact: true });
  await expect(mark).toBeEnabled();
  const edited = answerTo(page, "PUT", draftPath(draftId, "/text"));
  await mark.click();
  expect((await edited).status()).toBe(200);
  await expect(review.getByTitle(en["cv.text.removed_mark"], { exact: true })).toBeVisible();
  const draft = await getJson<DraftOut>(api, draftPath(draftId));
  expect(draft.status).toBe("text_review");
  expect(draft.stripped_text_preview ?? "").not.toContain(PLANTED_PHONE);
  expect(draft.edits.removed).toHaveLength(1);
  return draft;
}

/** [Approve text & extract profile]: the keyed approval (202), then the profile review once the draft is
 * `ready`. */
async function approve(page: Page, api: ApiContext, review: Locator, draftId: number): Promise<DraftOut> {
  const approved = answerTo(page, "POST", draftPath(draftId, "/text-review"));
  await review.getByRole("button", { name: templateMatch(en["cv.text.approve"]) }).click();
  const response = await approved;
  expect(response.status()).toBe(202);
  expect(await response.request().headerValue("idempotency-key")).toBeTruthy();
  await expect(page.getByRole("region", { name: en["cv.profile.title"] })).toBeVisible({ timeout: DRAFT_WITHIN_MS });
  return draftWhen(api, draftId, "ready", (draft) => draft.status === "ready" && draft.draft !== null);
}

/** Another value of `values` than `current`. */
function other<T>(values: readonly T[], current: T): T {
  const found = values.find((value) => value !== current);
  if (found === undefined) {
    throw new Error("no other value");
  }
  return found;
}

test("E3: a CV through the text review twice and the profile review, confirmed as v2 with its re-score", async ({ page }) => {
  test.setTimeout(TEXT_WITHIN_MS + 3 * DRAFT_WITHIN_MS + TARGETS_WITHIN_MS + 120_000);
  const api = await apiContext(page);
  try {
    const before = await getJson<ProfileListOut>(api, "/api/profile");
    expect(before.active?.version_no, "the setup's profile").toBe(NEXT_VERSION - 1);
    expect(before.open_draft, "no draft is open").toBeNull();
    const firstSkill = before.active?.profile?.skills[0]?.name;
    expect(firstSkill, "the active profile's first skill").toBeTruthy();
    const oldTuple = Number(await psql(ACTIVE_TUPLE));

    // The upload, and the strip report of the contacts the stripper removed.
    await page.goto("/settings#cv");
    const section = page.getByRole("region", { name: en["settings.section.cv"] });
    await expect(section.getByText(templateMatch(en["cv.active"], { version: String(NEXT_VERSION - 1) }))).toBeVisible();
    await expectNoSeriousA11yViolations(page);
    await section.getByLabel(en["cv.upload.browse"], { exact: true }).setInputFiles(CV_DOCX);
    const uploaded = answerTo(page, "POST", "/api/profile/cv");
    await section.getByRole("button", { name: en["cv.upload.submit"], exact: true }).click();
    const upload = await uploaded;
    expect(upload.status()).toBe(202);
    const { draft_id: draftId } = (await upload.json()) as { draft_id: number };
    const textReview = section.getByRole("region", { name: en["cv.text.title"] });
    await expect(textReview).toBeVisible({ timeout: TEXT_WITHIN_MS });
    const stripped = await draftWhen(api, draftId, "text_review", (draft) => draft.status === "text_review");
    expect(stripped.strip_report?.email ?? 0, "the invented e-mail is stripped").toBeGreaterThan(0);
    expect(stripped.strip_report?.phone ?? 0, "the planted phone is missed").toBe(0);
    expect(stripped.stripped_text_preview ?? "").toContain(PLANTED_PHONE);
    await expect(textReview.getByText(stripReportText(stripped.strip_report), { exact: true })).toBeVisible();
    await expectNoSeriousA11yViolations(page);

    // The missed phone marked, the text approved: `ready`.
    await markThePhone(page, api, textReview, draftId);
    await expectNoSeriousA11yViolations(page);
    await approve(page, api, textReview, draftId);
    const profileReview = section.getByRole("region", { name: en["cv.profile.title"] });
    await expectNoSeriousA11yViolations(page);

    // The text edited again from `ready`: the removal restored reopens the review, the extracted profile gone.
    await profileReview.getByText(en["cv.text.show"], { exact: true }).click();
    const restored = answerTo(page, "PUT", draftPath(draftId, "/text"));
    await profileReview.getByRole("button", { name: en["cv.text.restore_removed"], exact: true }).click();
    expect((await restored).status()).toBe(200);
    await expect(textReview).toBeVisible();
    await expect(profileReview).toBeHidden();
    const reopened = await getJson<DraftOut>(api, draftPath(draftId));
    expect([reopened.status, reopened.draft, reopened.validation_issues]).toEqual(["text_review", null, []]);
    expect(reopened.stripped_text_preview ?? "").toContain(PLANTED_PHONE);
    await expectNoSeriousA11yViolations(page);
    await markThePhone(page, api, textReview, draftId);
    const ready = await approve(page, api, textReview, draftId);
    await expectNoSeriousA11yViolations(page);

    // A skill other than the first and a language level edited, the draft saved.
    const profile = ready.draft;
    if (profile === null) {
      throw new Error("a ready draft holds its profile");
    }
    expect(profile.skills[0]?.name, "the extracted first skill is the active profile's").toBe(firstSkill);
    const [skill, language] = [profile.skills.at(1), profile.languages.at(1)];
    if (skill === undefined || language === undefined) {
      throw new Error("the extracted profile has a second skill and a second language");
    }
    const level = other(SKILL_LEVELS, skill.level);
    const cefr = other(CEFR_LEVELS, language.cefr);
    const form = profileReview.getByRole("form", { name: en["cv.profile.title"] });
    await form
      .getByRole("group", { name: en["cv.profile.skill_n"].replace("{{n}}", "2"), exact: true })
      .getByLabel(en["cv.profile.skill_level"], { exact: true })
      .selectOption(level);
    await form
      .getByRole("group", { name: en["cv.profile.language_n"].replace("{{n}}", "2"), exact: true })
      .getByLabel(en["cv.profile.cefr"], { exact: true })
      .selectOption(cefr);
    await expect(form.getByText(en["cv.profile.unsaved"], { exact: true })).toBeVisible();
    const saved = answerTo(page, "PUT", draftPath(draftId));
    await form.getByRole("button", { name: en["cv.profile.save"], exact: true }).click();
    expect((await saved).status()).toBe(200);
    await expect(form.getByText(en["cv.profile.unsaved"], { exact: true })).toBeHidden();
    const edited = (await getJson<DraftOut>(api, draftPath(draftId))).draft;
    const stored = [edited?.skills[0]?.name, edited?.skills[1]?.level, edited?.languages[1]?.cefr];
    expect(stored, "the saved draft's first skill, second skill's level and second language's level").toEqual([
      firstSkill,
      level,
      cefr,
    ]);
    await expectNoSeriousA11yViolations(page);

    // Confirm: the modal's sentence, then Confirm & re-score.
    const estimated = answerTo(page, "GET", draftPath(draftId, "/rescore-estimate"));
    await form.getByRole("button", { name: en["cv.profile.confirm"], exact: true }).click();
    const modal = page.getByRole("alertdialog", { name: en["cv.confirm.title"] });
    const estimate = (await (await estimated).json()) as RescoreEstimateOut;
    expect(estimate.next_version_no).toBe(NEXT_VERSION);
    expect(estimate.targets, "retained ads to re-score").toBeGreaterThan(1);
    expect([estimate.batch_targets, estimate.online_targets], "batch_bulk_rescore is off: all online").toEqual([
      0,
      estimate.targets,
    ]);
    // The figures as the UI formats them (`system/format.ts`, the en locale's en-GB), in the page's own Intl.
    const shown = await page.evaluate((e) => {
      const numbers = new Intl.NumberFormat("en-GB");
      const usd = new Intl.NumberFormat("en-GB", { style: "currency", currency: "USD", currencyDisplay: "narrowSymbol" });
      const percent = new Intl.NumberFormat("en-GB", { style: "percent" });
      return {
        targets: numbers.format(e.targets),
        budget: usd.format(Number(e.bulk_budget_usd)),
        share: percent.format(Number(e.bulk_share)),
        batch: numbers.format(e.batch_targets),
        online: numbers.format(e.online_targets),
        per_day: numbers.format(e.online_per_day),
        spend: usd.format(Number(e.month_spend)),
        cap: usd.format(Number(e.cap)),
      };
    }, estimate);
    const sentence = templateMatch(en["cv.confirm.sentence_other"], { version: String(NEXT_VERSION), ...shown });
    await expect(modal.getByText(sentence)).toBeVisible();
    await expectNoSeriousA11yViolations(page);
    const confirmed = answerTo(page, "POST", draftPath(draftId, "/confirm"));
    await modal.getByRole("button", { name: en["cv.confirm.confirm"], exact: true }).click();
    const confirm = await confirmed;
    expect(confirm.status()).toBe(201);
    expect(await confirm.request().headerValue("idempotency-key")).toBeTruthy();
    const out = (await confirm.json()) as ConfirmOut;
    await expect(modal).toBeHidden();
    await expect(section.getByText(templateMatch(en["cv.active"], { version: String(NEXT_VERSION) }))).toBeVisible();
    await expectNoSeriousA11yViolations(page);

    // v2 is active with the edits, and a new tuple whose targets the cards carry, pending.
    const after = await getJson<ProfileListOut>(api, "/api/profile");
    expect([after.active?.id, after.active?.version_no, after.open_draft]).toEqual([
      out.profile_version_id,
      NEXT_VERSION,
      null,
    ]);
    const active = after.active?.profile;
    const kept = [active?.skills[0]?.name, active?.skills[1]?.level, active?.languages[1]?.cefr];
    expect(kept, "v2's first skill, second skill's level and second language's level").toEqual([firstSkill, level, cefr]);
    const newTuple = Number(await psql(ACTIVE_TUPLE));
    expect(newTuple, "the confirm activates a new tuple").not.toBe(oldTuple);
    const spend = await getJson<SpendOut>(api, "/api/system/spend");
    expect(spend.bulk_operation?.llm_tuple_id, "the new tuple's bulk operation").toBe(newTuple);
    await waitFor(
      async () => {
        const [status, targets] = (await psql(TARGETS_OF(newTuple))).split("|");
        if (status === "failed") {
          throw new Error(`create_targets_for_tuple of tuple ${String(newTuple)} failed`);
        }
        return status === "succeeded" && Number(targets) > 0 ? true : null;
      },
      TARGETS_WITHIN_MS,
      `the targets of tuple ${String(newTuple)}`,
    );
    const lastOldTarget = Number(await psql(LAST_TARGET_OF(oldTuple)));
    const page1 = await getJson<JobsPageOut>(api, "/api/jobs?limit=100");
    const targeted = page1.items.filter((card) => card.target !== null);
    expect(targeted.length, "cards with a target of the active tuple").toBeGreaterThan(0);
    const onOldTargets = targeted.filter((card) => (card.target?.id ?? 0) <= lastOldTarget);
    expect(onOldTargets.map((card) => card.job_id), "cards on a target of the old tuple").toEqual([]);
    const unpublished = page1.items.filter(
      (card) => card.score !== null && !(card.target?.state ?? "").startsWith("published_"),
    );
    expect(unpublished.map((card) => card.job_id), "cards showing a result their target has not published").toEqual([]);
    expect(targeted.filter((card) => card.target?.state === "pending").length, "pending targets").toBeGreaterThan(0);
  } finally {
    await api.dispose();
  }
});
