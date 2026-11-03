import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import { fakeApi, json, problem, requestsTo, statusOut } from "../test/api";
import { NOW, renderApp, settled } from "../test/app";

type RunOut = components["schemas"]["RunOut"];

afterEach(async () => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function run(id: number, status: RunOut["status"], extra: Partial<RunOut> = {}): RunOut {
  return {
    id,
    kind: "collect",
    trigger: "manual",
    status,
    status_reason: null,
    scheduled_slot: null,
    include_paid: false,
    attempt_no: 1,
    queued_at: "2026-11-03T07:00:00Z",
    started_at: status === "queued" ? null : "2026-11-03T07:00:02Z",
    finished_at: status === "queued" || status === "running" ? null : "2026-11-03T07:00:40Z",
    ...extra,
  };
}

async function header(): Promise<HTMLElement> {
  const banner = await screen.findByRole("banner");
  await within(banner).findByText(en["header.last_run"]);
  return banner;
}

function liveText(): string | null | undefined {
  return document.querySelector("[aria-live=polite][role=status]")?.textContent;
}

describe("RunNowPopover", () => {
  it("the checkbox names the units", async () => {
    const api = fakeApi({ signedIn: true });
    api.status = statusOut({ run_now: { jsearch_units: 1234, serpapi_fallback_units: 0 } });
    renderApp("/");
    const strip = await header();
    await userEvent.setup().click(within(strip).getByRole("button", { name: en["run_now.open"] }));
    const dialog = await screen.findByRole("dialog", { name: en["run_now.title"] });
    const box = within(dialog).getByRole("checkbox", { name: "Include paid sources (uses ~1,234 JSearch units)" });
    expect((box as HTMLInputElement).checked).toBe(false);
  });

  it("the popover injects no style element", async () => {
    fakeApi({ signedIn: true });
    renderApp("/");
    const strip = await header();
    await userEvent.setup().click(within(strip).getByRole("button", { name: en["run_now.open"] }));
    expect(await screen.findByRole("dialog", { name: en["run_now.title"] })).toBeTruthy();
    expect(document.querySelectorAll("style")).toHaveLength(0); // the CSP refuses an injected <style>
  });

  it("Esc closes the popover and gives the focus back to its trigger", async () => {
    fakeApi({ signedIn: true });
    renderApp("/");
    const strip = await header();
    const user = userEvent.setup();
    const trigger = within(strip).getByRole("button", { name: en["run_now.open"] });
    await user.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: en["run_now.title"] });
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(document.activeElement).toBe(trigger);
  });

  it("Start sends a keyed POST with include_paid", async () => {
    const api = fakeApi({ signedIn: true });
    // after the POST, the status read is held, and the server's status has no active run yet: the label can
    // come from the response's run only
    let release: (() => void) | null = null;
    api.routes.set("POST /api/runs", () => {
      api.routes.set("GET /api/system/status", () =>
        new Promise<Response>((resolve) => {
          release = () => { resolve(json(api.status)); };
        }),
      );
      return json({ run: run(9, "queued", { include_paid: true }), created: true }, 202);
    });
    renderApp("/");
    const strip = await header();
    const user = userEvent.setup();
    await user.click(within(strip).getByRole("button", { name: en["run_now.open"] }));
    const dialog = await screen.findByRole("dialog", { name: en["run_now.title"] });
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(within(dialog).getByRole("button", { name: en["run_now.start"] }));
    const [post] = requestsTo(api, "POST /api/runs");
    expect(JSON.parse(post.body ?? "null")).toEqual({ include_paid: true });
    expect(post.headers.get("Idempotency-Key")).toMatch(UUID);
    expect(post.headers.get("X-JH-Request")).toBe("1");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(api.status.active_run_id).toBeNull();
    expect(within(strip).getByRole("button", { name: en["run_now.in_progress"] })).toBeTruthy();
    await settled();
    expect(liveText()).toBe(en["run_now.started"]);
    // the status is being read again at once; the drawer is not opened by itself
    expect(requestsTo(api, "GET /api/system/status").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByRole("dialog")).toBeNull();
    // the next status names the run: the label now comes from it
    api.status = statusOut({ active_run_id: 9 });
    await act(async () => {
      release?.();
      await Promise.resolve();
    });
    await settled();
    expect(within(strip).getByRole("button", { name: en["run_now.in_progress"] })).toBeTruthy();
  });

  it("a successful Start clears Include paid sources, a failed one keeps it", async () => {
    const api = fakeApi({ signedIn: true });
    // the retry queues run 9, which has ended by the next status read (a fixture run is short)
    const answers = [() => problem(503, "internal"), () => json({ run: run(9, "queued"), created: true }, 202)];
    api.routes.set("POST /api/runs", () => answers.shift()?.() ?? problem(500, "internal"));
    renderApp("/");
    const strip = await header();
    const user = userEvent.setup();
    await user.click(within(strip).getByRole("button", { name: en["run_now.open"] }));
    let dialog = await screen.findByRole("dialog", { name: en["run_now.title"] });
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(within(dialog).getByRole("button", { name: en["run_now.start"] }));
    const failed = await within(dialog).findByRole("alert");
    expect(within(dialog).getByRole<HTMLInputElement>("checkbox").checked).toBe(true); // the same action
    await user.click(within(failed).getByRole("button", { name: en["action.retry"] }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(requestsTo(api, "POST /api/runs").map((r) => JSON.parse(r.body ?? "null") as unknown)).toEqual([
      { include_paid: true },
      { include_paid: true },
    ]);
    await user.click(await within(strip).findByRole("button", { name: en["run_now.open"] }));
    dialog = await screen.findByRole("dialog", { name: en["run_now.title"] });
    expect(within(dialog).getByRole<HTMLInputElement>("checkbox").checked).toBe(false);
  });

  it("closes for good when a run starts elsewhere, and does not reopen when it ends", async () => {
    const api = fakeApi({ signedIn: true });
    const app = renderApp("/");
    const strip = await header();
    await userEvent.setup().click(within(strip).getByRole("button", { name: en["run_now.open"] }));
    expect(await screen.findByRole("dialog", { name: en["run_now.title"] })).toBeTruthy();
    const refetch = async () => {
      await act(async () => {
        await app.queryClient.refetchQueries({ queryKey: ["system", "status"] });
      });
      await settled();
    };
    api.status = statusOut({ active_run_id: 9 }); // a scheduled run, or another tab's
    await refetch();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(within(strip).getByRole("button", { name: en["run_now.in_progress"] })).toBeTruthy();
    api.status = statusOut({ active_run_id: null }); // it ended
    await refetch();
    expect(within(strip).getByRole("button", { name: en["run_now.open"] })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement?.closest("[role=dialog]") ?? null).toBeNull();
  });

  it("an already active run is announced as such", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/runs", () => json({ run: run(8, "running"), created: false }, 202));
    renderApp("/");
    const strip = await header();
    const user = userEvent.setup();
    await user.click(within(strip).getByRole("button", { name: en["run_now.open"] }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: en["run_now.start"] }));
    await waitFor(() => {
      expect(liveText()).toBe(en["run_now.already_running"]);
    });
    expect(JSON.parse(requestsTo(api, "POST /api/runs")[0].body ?? "null")).toEqual({ include_paid: false });
  });

  it("a retry after a 503 reuses the key", async () => {
    const api = fakeApi({ signedIn: true });
    const answers = [() => problem(503, "internal"), () => problem(409, "idempotency_in_progress")];
    api.routes.set("POST /api/runs", () => answers.shift()?.() ?? problem(500, "internal"));
    renderApp("/");
    const strip = await header();
    const user = userEvent.setup();
    await user.click(within(strip).getByRole("button", { name: en["run_now.open"] }));
    const dialog = await screen.findByRole("dialog", { name: en["run_now.title"] });
    await user.click(within(dialog).getByRole("button", { name: en["run_now.start"] }));
    const failed = await within(dialog).findByRole("alert");
    expect(failed.textContent).toContain(en["problem.internal"]);
    await user.click(within(failed).getByRole("button", { name: en["action.retry"] }));
    await waitFor(() => {
      expect(within(dialog).getByRole("alert").textContent).toContain(en["problem.idempotency_in_progress"]);
    });
    const [first, retry] = requestsTo(api, "POST /api/runs");
    expect(first.headers.get("Idempotency-Key")).toMatch(UUID);
    expect(retry.headers.get("Idempotency-Key")).toBe(first.headers.get("Idempotency-Key"));
    expect(retry.body).toBe(first.body);
  });

  it("an active run shows Run in progress and opens the report polled every 10 s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: NOW });
    const api = fakeApi({ signedIn: true });
    api.status = statusOut({ active_run_id: 9 });
    const answers = [
      () => json({ run: run(9, "queued"), report: null, partial_causes: [] }),
      () => json({ run: run(9, "running"), report: null, partial_causes: [] }),
      () => json({ run: run(9, "running"), report: null, partial_causes: [] }), // the same status again
      () => {
        api.status = statusOut({ active_run_id: null });
        return json({
          run: run(9, "partial", { status_reason: "fetch_failed" }),
          report: { identity: { jobs_created: 1234 } },
          partial_causes: ["fetch_failed"],
        });
      },
    ];
    api.routes.set("GET /api/runs/9", () => answers.shift()?.() ?? problem(500, "internal"));
    renderApp("/");
    const strip = await header();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) });
    const view = within(strip).getByRole("button", { name: en["run_now.in_progress"] });
    await user.click(view);
    const drawer = await screen.findByRole("dialog", { name: "Run 9" });
    expect(await within(drawer).findByText(en["run_status.queued"])).toBeTruthy();
    expect(within(drawer).getByText(en["run_report.no_report"])).toBeTruthy();
    const reads = () => requestsTo(api, "GET /api/runs/9");
    expect(reads()).toHaveLength(1);
    expect(reads()[0].headers.get("X-JH-Background")).toBe("1");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(await within(drawer).findByText(en["run_status.running"])).toBeTruthy();
    expect(reads()).toHaveLength(2);
    await settled();
    expect(liveText()).toBe(`Run 9: ${en["run_status.running"]}`);

    // a poll that changes nothing announces nothing: the region, cleared, stays empty
    const live = document.querySelector("[aria-live=polite][role=status]");
    if (live === null) {
      throw new Error("no live region");
    }
    live.textContent = "";
    // the clock moves timer by timer up to that poll, then in 50 ms steps, each in an act of its own, so that
    // React commits between them: an announcement empties the region, then fills it 100 ms later
    for (let i = 0; i < 20 && reads().length < 3; i++) {
      await act(async () => {
        await vi.advanceTimersToNextTimerAsync();
      });
    }
    expect(reads()).toHaveLength(3);
    for (let i = 0; i < 4; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50);
      });
    }
    expect(within(drawer).getByText(en["run_status.running"])).toBeTruthy();
    expect(liveText()).toBe("");

    const statusReads = requestsTo(api, "GET /api/system/status").length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(await within(drawer).findByText(en["run_status.partial"])).toBeTruthy();
    expect(reads()).toHaveLength(4);
    expect(reads()[3].headers.get("X-JH-Background")).toBe("1");
    // the run ended: the status is read at once, the header's button is Run now again, the drawer stays
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/system/status").length).toBeGreaterThan(statusReads);
    });
    await waitFor(() => {
      expect(within(strip).getByRole("button", { name: en["run_now.open"] })).toBeTruthy();
    });
    expect(screen.getByRole("dialog", { name: "Run 9" })).toBe(drawer);
    expect(within(drawer).getByText("1,234")).toBeTruthy();
    await settled();
    expect(liveText()).toBe(`Run 9: ${en["run_status.partial"]}`);

    // a terminal run is not polled any more
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(reads()).toHaveLength(4);
  });
});
