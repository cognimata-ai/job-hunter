// E17 (spec §18.7, §15.7, §10.6; plan M4 Task 34, resolution e): System → Needs attention. The setup's seeded
// rejected item (step 8) is listed as replayable; Replay queues it, and the response leaves the block (its item
// `linked`, the block's count one lower). Then a quarantined batch: the fake Batches API scripted to an ambiguous
// create (`writeBatchScript`), and one settings save through the API turns on `scoring.batch_bulk_rescore`, sets
// `scoring.batch_min_targets` to 20 (the fixtures retain fewer than the default 200, Ruling T18-a) and the A grid
// (generate, revise and critic on claude-opus-5-5/medium; the stored label is the grid's normal form, so the
// label is left as it is), with `llm.overrun_buffer_usd` raised to the A grid's §6.9.1 bound where it is below
// (`overrunBufferBound`; the dry-run refuses less): an `llm_tuple` re-score, whose batch is sent and ends
// `submit_ambiguous`; the script is removed then, so no later batch is quarantined. The quarantined block shows
// its reserved amount; Release & re-score asks first, stating that amount, and the batch is `released`, its
// targets requeued (`pending`, or already in the batch the re-score sends). At the end the script is removed
// (again: after a failure it may still be there) and the four settings restored.
import type { Locator, Page } from "@playwright/test";
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { answerTo, type ApiContext, apiContext, getJson } from "../support/api";
import { en, expect, filled, templateMatch, test } from "../support/fixtures";
import { pageUsd, readSettings, type SaveOut, saveSettings, type Settings, withRestore } from "../support/settings";
import { overrunBufferBound, psql, seededRejectedItem, waitFor, writeBatchScript } from "../support/stack";

type AttentionOut = components["schemas"]["AttentionOut"];
type QuarantinedBatchOut = components["schemas"]["QuarantinedBatchOut"];

const REPLAYED_WITHIN_MS = 60_000;
const ATTENTION_POLL_MS = 90_000; // Needs attention is read again every 60 s
const QUARANTINED_WITHIN_MS = 300_000;
const BATCH_MIN_TARGETS = 20;
const GRID_A = { model: "claude-opus-5-5", effort: "medium" } as const; // `settings/presets.json`'s A
const GRID_NODES = ["generate", "revise", "critic"] as const;
const AMBIGUOUS = { create: "ambiguous_not_created", listing: "empty" } as const;

// The replay and the quarantined batch are one-shot per stack: a retry could only fail on their spent
// preconditions (Ruling T33-b).
test.describe.configure({ retries: 0 });

function attention(api: ApiContext): Promise<AttentionOut> {
  return getJson<AttentionOut>(api, "/api/system/needs-attention");
}

/** A block of Needs attention, by its section's name (its heading also holds its count). */
function block(page: Page, section: string): Locator {
  const named = new RegExp(`^${section.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")} [\\d,]+$`);
  return page.getByRole("region", { name: en["attention.title"] }).getByRole("region", { name: named });
}

type Scoring = Settings["scoring"];
/** The values E17's save changes, read at the start and restored at the end. */
interface Restore {
  batch_bulk_rescore: Scoring["batch_bulk_rescore"];
  batch_min_targets: Scoring["batch_min_targets"];
  model_policy: Scoring["model_policy"];
  overrun_buffer_usd: Settings["llm"]["overrun_buffer_usd"];
}

/** `payload` with batched bulk re-scores from 20 retained revisions and the A grid (the label left as it is). */
function withGridA(payload: Settings): void {
  payload.scoring.batch_bulk_rescore = true;
  payload.scoring.batch_min_targets = BATCH_MIN_TARGETS;
  for (const node of GRID_NODES) {
    payload.scoring.model_policy.nodes[node] = { ...GRID_A };
  }
}

test("E17: Replay of the seeded rejected item, and Release & re-score of a quarantined batch", async ({ page }) => {
  test.setTimeout(QUARANTINED_WITHIN_MS + 300_000);
  const api = await apiContext(page);
  try {
    const seeded = await seededRejectedItem();
    expect(seeded, "the setup's seeded rejected item (step 8)").not.toBeNull();
    const response = seeded?.response ?? 0;
    const listed = (await attention(api)).raw_items;
    const at = listed.items.findIndex((item) => item.raw_response_id === response);
    expect(
      listed.items[at]?.replayable,
      `the seeded response ${String(response)} listed as replayable: E17's Replay needs a fresh stack`,
    ).toBe(true);

    await page.goto("/settings#system");
    const rawItems = block(page, en["attention_section.raw_items"]);
    await expect(rawItems.getByRole("heading")).toHaveText(`${en["attention_section.raw_items"]} ${String(listed.count)}`);
    await expectNoSeriousA11yViolations(page);
    await test.step("Replay: the response leaves the block, its item linked", async () => {
      const replayed = answerTo(page, "POST", "/api/system/raw-items/replay");
      await rawItems.getByRole("listitem").nth(at).getByRole("button", { name: en["attention.replay"], exact: true }).click();
      const answer = await replayed;
      expect(answer.status(), `the replay of response ${String(response)}`).toBe(202);
      expect(answer.request().postDataJSON()).toEqual({ raw_response_id: response });
      await waitFor(
        async () => ((await attention(api)).raw_items.items.some((item) => item.raw_response_id === response) ? null : true),
        REPLAYED_WITHIN_MS,
        `response ${String(response)} out of Needs attention`,
      );
      expect(await psql(`SELECT disposition FROM raw_items WHERE id = ${String(seeded?.item ?? 0)}`)).toBe("linked");
      await expect(rawItems.getByRole("heading")).toHaveText(`${en["attention_section.raw_items"]} ${String(listed.count - 1)}`, {
        timeout: ATTENTION_POLL_MS,
      });
      await expectNoSeriousA11yViolations(page);
    });

    const active = (await readSettings(api)).payload;
    const before: Restore = {
      batch_bulk_rescore: active.scoring.batch_bulk_rescore,
      batch_min_targets: active.scoring.batch_min_targets,
      model_policy: active.scoring.model_policy,
      overrun_buffer_usd: active.llm.overrun_buffer_usd,
    };
    const restore = async () => {
      await writeBatchScript(null);
      const what = "the batch settings, the grid and the overrun buffer read at the start";
      const restored = await saveSettings(api, what, (p) => {
        p.scoring.batch_bulk_rescore = before.batch_bulk_rescore;
        p.scoring.batch_min_targets = before.batch_min_targets;
        p.scoring.model_policy = before.model_policy;
        p.llm.overrun_buffer_usd = before.overrun_buffer_usd;
      });
      expect(restored.response.status(), what).toBe(201);
    };
    const newest = Number(await psql("SELECT coalesce(max(id), 0) FROM batch_submissions"));
    await withRestore(async () => {
      await writeBatchScript(AMBIGUOUS);
      const batch = await test.step("the A grid with batched bulk re-scores: a quarantined batch", async () => {
        const draft = structuredClone(active);
        withGridA(draft);
        // the A grid's §6.9.1 bound (its costliest model, `batch_max_items` generator items) may pass the buffer
        const bound = await overrunBufferBound(draft);
        const buffer = Number(bound) > Number(before.overrun_buffer_usd) ? bound : before.overrun_buffer_usd;
        const saved = await saveSettings(api, `the A grid, batch_bulk_rescore, batch_min_targets 20, buffer ${buffer}`, (p) => {
          withGridA(p);
          p.llm.overrun_buffer_usd = buffer;
        });
        expect(saved.response.status(), "the save of the A grid").toBe(201);
        expect(((await saved.response.json()) as SaveOut).version.effect_classes).toContain("llm_tuple");
        return waitFor(
          async (): Promise<QuarantinedBatchOut | null> =>
            (await attention(api)).quarantined_batches.items.find(
              (item) => item.submission_id > newest && item.state === "submit_ambiguous",
            ) ?? null,
          QUARANTINED_WITHIN_MS,
          `a submit_ambiguous batch after submission ${String(newest)}`,
        );
      });
      // The script has quarantined its batch: from here on every batch follows the default script, so the
      // re-score that Release starts is not quarantined in turn.
      await writeBatchScript(null);

      await page.reload();
      const quarantined = block(page, en["attention_section.quarantined_batches"]);
      const item = quarantined.getByRole("listitem").filter({
        hasText: filled(en["attention.batch_title"], { id: String(batch.submission_id) }),
      });
      const usd = await pageUsd(page, batch.reserved_usd);
      const meta = batch.request_count === 1 ? en["attention.batch_meta_one"] : en["attention.batch_meta_other"];
      await expect(item.getByText(templateMatch(meta, { usd, value: String(batch.request_count) }))).toBeVisible();
      await expectNoSeriousA11yViolations(page);

      await item.getByRole("button", { name: en["attention.release"], exact: true }).click();
      const confirm = page.getByRole("alertdialog", { name: en["attention.release_title"] });
      await expect(confirm).toHaveAccessibleDescription(filled(en["attention.release_body"], { usd }));
      await expectNoSeriousA11yViolations(page);
      const released = answerTo(page, "POST", `/api/batches/${String(batch.submission_id)}/release`);
      await confirm.getByRole("button", { name: en["attention.release_confirm"], exact: true }).click();
      const answer = await released;
      expect(answer.status(), `the release of batch ${String(batch.submission_id)}`).toBe(202);
      expect(await answer.json()).toMatchObject({ submission_id: batch.submission_id, state: "released" });
      // Release requeues the batch's targets as `pending`, and the re-score it starts may send them in a new
      // batch at once: each target is out of the released batch, `pending` or `in_batch` in a later one.
      const requeued = await psql(
        "SELECT s.state || '|' || count(DISTINCT t.id) FILTER (WHERE t.state = 'pending' OR (t.state = 'in_batch' " +
          "AND t.batch_submission_id > s.id)) || '|' || count(DISTINCT t.id) FROM batch_submissions s " +
          "JOIN batch_items i ON i.submission_id = s.id JOIN score_targets t ON t.id = i.target_id " +
          `WHERE s.id = ${String(batch.submission_id)} GROUP BY s.state`,
      );
      const targets = String(batch.request_count);
      expect(requeued, `batch ${String(batch.submission_id)}'s state | its targets requeued | its targets`).toBe(
        `released|${targets}|${targets}`,
      );
      await expect(page.getByText(en["attention.released"], { exact: true })).toBeVisible();
      await expectNoSeriousA11yViolations(page);
    }, restore);
    const after = (await readSettings(api)).payload;
    expect({
      batch_bulk_rescore: after.scoring.batch_bulk_rescore,
      batch_min_targets: after.scoring.batch_min_targets,
      model_policy: after.scoring.model_policy,
      overrun_buffer_usd: after.llm.overrun_buffer_usd,
    }).toEqual(before);
  } finally {
    await api.dispose();
  }
});
