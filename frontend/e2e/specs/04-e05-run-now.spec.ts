// E5 (spec §18.7, §14.1's stored response, §15.1; plan M4 Task 33, resolution d): Run now with paid sources
// unticked queues a manual run (202, `created = true`, the request's `Idempotency-Key` read as it is sent); a
// second Start while that run is active, with a new key, answers 202 with the same run and `created = false`; the
// header reads "Run in progress (view)" and its drawer shows the run's progress until it ends (`succeeded`, or
// `partial` with its causes); then the first key, replayed with the same body, returns the stored answer byte for
// byte and starts no run. Manual runs are counted (`trigger = 'manual'`): the stack's schedule may start a
// collection of its own (`schedule` or `catch_up`), which neither fails the spec nor hides a run a replay made.
import type { APIResponse, Request } from "@playwright/test";
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { apiContext, getJson } from "../support/api";
import { en, expect, templateMatch, test } from "../support/fixtures";
import { psql, waitFor, workerOnBothFakes } from "../support/stack";

type RunNowOut = components["schemas"]["RunNowOut"];
type RunDetailOut = components["schemas"]["RunDetailOut"];
type RunStatus = components["schemas"]["RunOut"]["status"];

const BODY = { include_paid: false } as const;
const ACTIVE: readonly RunStatus[] = ["queued", "running"];
const ENDED_WELL: readonly RunStatus[] = ["succeeded", "partial"];
const IDLE_WITHIN_MS = 300_000; // a collection the schedule started ends first
const RUN_WITHIN_MS = 300_000;
const DRAWER_POLL_MS = 30_000; // the drawer reads an active run every 10 s
// The manual runs: their count and the newest id (a replay that queued a run would move both).
const MANUAL_RUNS = "SELECT count(*) || '|' || coalesce(max(id), 0) FROM runs WHERE trigger = 'manual'";
const ACTIVE_RUNS = "SELECT count(*) FROM runs WHERE status IN ('queued', 'running')";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PRINTABLE_ASCII = /^[\x20-\x7e]+$/; // a compact JSON body: its text is its bytes

// Resolution (d): never retried or re-timed into passing, CI's retry included (Ruling T33-b).
test.describe.configure({ retries: 0 });

/** A run status as the UI names it (`run_status.<s>`). */
function statusText(status: RunStatus): string {
  return en[`run_status.${status}` as const];
}

function runPath(id: number): string {
  return `/api/runs/${String(id)}`;
}

function isRunNow(request: Request): boolean {
  return request.method() === "POST" && new URL(request.url()).pathname === "/api/runs";
}

async function runNowAnswer(response: APIResponse): Promise<RunNowOut> {
  expect(response.status(), "POST /api/runs").toBe(202);
  return (await response.json()) as RunNowOut;
}

test("E5: Run now queues one run, a second Start joins it, and the first key replays its stored answer", async ({
  page,
}) => {
  test.setTimeout(IDLE_WITHIN_MS + RUN_WITHIN_MS + 120_000);
  expect(await workerOnBothFakes(), "the worker runs on the fake LLM and the fake sources: no run is posted").toBe(true);
  await waitFor(async () => ((await psql(ACTIVE_RUNS)) === "0" ? true : null), IDLE_WITHIN_MS, "no active collection");
  const api = await apiContext(page);
  try {
    await page.goto("/");
    await page.getByRole("button", { name: en["run_now.open"], exact: true }).click();
    const popover = page.getByRole("dialog", { name: en["run_now.title"] });
    const paid = popover.getByRole("checkbox");
    await expect(paid).not.toBeChecked();
    await expect(popover.getByText(templateMatch(en["run_now.include_paid"]))).toBeVisible();
    await expectNoSeriousA11yViolations(page);

    // Start: the request's key as the SPA sends it, and its answer.
    const sent = page.waitForRequest(isRunNow);
    const answered = page.waitForResponse((response) => isRunNow(response.request()));
    await popover.getByRole("button", { name: en["run_now.start"] }).click();
    const request = await sent;
    const firstKey = (await request.allHeaders())["idempotency-key"] ?? "";
    expect(firstKey).toMatch(UUID);
    expect(request.postDataJSON()).toEqual(BODY);
    const response = await answered;
    expect(response.status()).toBe(202);
    const firstText = await response.text();
    const first = JSON.parse(firstText) as RunNowOut;
    const firstSaid = `run ${String(first.run.id)} (${first.run.trigger}, ${first.run.status})`;
    expect(first.created, `Start answered ${firstSaid}`).toBe(true);
    expect(first.run, `Start answered ${firstSaid}`).toMatchObject({ trigger: "manual", include_paid: false });

    // A second Start at once, while the run is active: a new key, the same body.
    const second = await runNowAnswer(await api.post("/api/runs", { data: BODY, headers: { "Idempotency-Key": api.key() } }));
    const joined = second.run.id === first.run.id && !second.created && ACTIVE.includes(second.run.status);
    // on a mismatch, where the first run is now (read only then, so the second Start follows the first at once)
    const firstNow = joined ? "" : `, ${(await getJson<RunDetailOut>(api, runPath(first.run.id))).run.status} now`;
    const state =
      `the second Start found run ${String(second.run.id)} ${second.run.status} (created ${String(second.created)}); ` +
      `the first Start's was ${firstSaid}${firstNow}`;
    expect(second.run.id, state).toBe(first.run.id);
    expect(second.created, state).toBe(false);
    expect(ACTIVE, state).toContain(second.run.status);

    // The header: "Run in progress (view)", and its drawer with the run's progress.
    const inProgress = page.getByRole("button", { name: en["run_now.in_progress"], exact: true });
    await expect(inProgress).toBeVisible();
    await expectNoSeriousA11yViolations(page);
    await inProgress.click();
    const drawer = page.getByRole("dialog", { name: templateMatch(en["run_report.title"], { id: String(first.run.id) }) });
    const status = drawer.locator(`dt:text-is("${en["run_report.status"]}") + dd`);
    const activeText = new RegExp(`^(${ACTIVE.map(statusText).join("|")})$`);
    await expect(status).toHaveText(activeText);
    await expect(drawer.getByText(en["run_report.no_report"], { exact: true })).toBeVisible();
    await expectNoSeriousA11yViolations(page);

    // The run ends, succeeded or partial with its causes; the drawer follows.
    const ended = await waitFor(
      async () => {
        const detail = await getJson<RunDetailOut>(api, runPath(first.run.id));
        return ACTIVE.includes(detail.run.status) ? null : detail;
      },
      RUN_WITHIN_MS,
      `the end of run ${String(first.run.id)}`,
    );
    expect(ENDED_WELL, `run ${String(first.run.id)} ended ${ended.run.status} (${String(ended.run.status_reason)})`).toContain(
      ended.run.status,
    );
    if (ended.run.status === "partial") {
      expect(ended.partial_causes.length, "a partial run names its causes").toBeGreaterThan(0);
    }
    await expect(status).toHaveText(statusText(ended.run.status), { timeout: DRAWER_POLL_MS });
    await expect(drawer.getByRole("heading", { name: en["run_report.section.sources"] })).toBeVisible();
    for (const cause of ended.partial_causes) {
      await expect(drawer.getByRole("listitem").filter({ hasText: en[`partial.${cause}` as const] })).toBeVisible();
    }
    await expectNoSeriousA11yViolations(page);
    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();

    // The first key again, with the same body: the stored answer byte for byte, and no run.
    const before = await psql(MANUAL_RUNS);
    const replay = await api.post("/api/runs", { data: BODY, headers: { "Idempotency-Key": firstKey } });
    expect(replay.status()).toBe(202);
    const replayText = await replay.text();
    expect(firstText).toMatch(PRINTABLE_ASCII);
    expect(replayText).toBe(firstText);
    const replayed = JSON.parse(replayText) as RunNowOut;
    expect([replayed.run.id, replayed.created]).toEqual([first.run.id, true]);
    expect(await psql(MANUAL_RUNS), "manual runs (count|newest id) around the replay").toBe(before);
  } finally {
    await api.dispose();
  }
});
