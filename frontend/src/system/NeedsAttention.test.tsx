import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import codes from "../i18n/codes.json";
import en from "../i18n/en.json";
import { attentionOut, type FakeApi, fakeApi, json, problem, requestsTo } from "../test/api";
import { renderApp } from "../test/app";

type AttentionOut = components["schemas"]["AttentionOut"];

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

// The blocks in `AttentionOut`'s order (observability/attention.py; `codes.json`'s `order.attention_section`,
// which test_i18n_codes.py holds to the model): no failed-purges block (U1).
const ORDER = codes.order.attention_section as (keyof AttentionOut)[];

// Needs attention's job links are the router's: these tests render the app at Profile & Settings.
afterEach(() => {
  vi.restoreAllMocks();
});

function attention(): HTMLElement {
  const element = document.getElementById("needs-attention");
  if (element === null) {
    throw new Error("no needs-attention part");
  }
  return element;
}

/** The block of the section `name` (its heading starts with the section's title). */
async function block(name: keyof AttentionOut): Promise<HTMLElement> {
  const title = en[`attention_section.${name}`];
  const part = await screen.findByRole("region", { name: en["attention.title"] });
  const heading = await within(part).findByRole("heading", { level: 4, name: new RegExp(`^${title.replace(/[()]/g, "\\$&")}`) });
  const section = heading.closest("section");
  if (section === null) {
    throw new Error(`no block for ${name}`);
  }
  return section;
}

/** The item of `container` holding `text`. */
function item(container: HTMLElement, text: string | RegExp): HTMLElement {
  const found = within(container).getByText(text).closest("li");
  if (found === null) {
    throw new Error(`no item with ${String(text)}`);
  }
  return found;
}

function bodyOf(api: FakeApi, call: string): unknown {
  const request = requestsTo(api, call).at(-1);
  return request?.body == null ? null : JSON.parse(request.body);
}

function rawItems(replayable: boolean): AttentionOut["raw_items"] {
  return {
    count: 1,
    items: [
      {
        raw_response_id: 7,
        source_id: "arbeitnow",
        instance_label: "Arbeitnow feed",
        fetched_at: "2026-11-03T05:00:00Z",
        rejected: 3,
        reasons: ["missing_title", "unparsable_date"],
        replayable,
      },
    ],
  };
}

const QUARANTINED: AttentionOut["quarantined_batches"] = {
  count: 1,
  items: [
    {
      submission_id: 3,
      state: "submit_ambiguous",
      reserved_usd: "0.840000",
      request_count: 40,
      submit_started_at: "2026-11-03T04:00:00Z",
      last_reconcile_at: null,
      unread_candidates: 0,
    },
  ],
};

const RETRIEVE_FAILING: AttentionOut["retrieve_failing"] = {
  count: 2,
  items: [
    {
      submission_id: 5,
      provider_batch_id: "msgbatch_early",
      expires_at: "2026-11-03T05:00:00Z",
      alert_id: 12,
      reserved_usd: "2.100000",
      releasable: false,
    },
    {
      submission_id: 6,
      provider_batch_id: "msgbatch_late",
      expires_at: "2026-11-02T05:00:00Z",
      alert_id: 13,
      reserved_usd: "1.250000",
      releasable: true,
    },
  ],
};

describe("NeedsAttention", () => {
  it("shows one block per section in the module's order, with no failed-purges block", async () => {
    fakeApi({ signedIn: true });
    renderApp("/settings#system");
    await block("raw_items");
    const headings = within(attention()).getAllByRole("heading", { level: 4 }).map((h) => h.textContent);
    expect(headings).toHaveLength(ORDER.length);
    ORDER.forEach((name, i) => {
      expect(headings[i]?.startsWith(en[`attention_section.${name}`]), name).toBe(true);
    });
    expect(headings.some((h) => /purge|lösch/i.test(h))).toBe(false);
    // an empty section says so in one line
    expect(within(await block("identity_conflicts")).getByText(en["attention.nothing"])).toBeTruthy();
    expect(within(await block("paused_lane")).getByText(en["attention.nothing"])).toBeTruthy();
  });

  it('"and n more" when the count exceeds the items', async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({ raw_items: { ...rawItems(true), count: 53 } });
    renderApp("/settings#system");
    const raw = await block("raw_items");
    expect(within(raw).getByRole("heading", { level: 4 }).textContent).toContain("53");
    expect(within(raw).getAllByRole("listitem")).toHaveLength(1);
    expect(within(raw).getByText("and 52 more")).toBeTruthy();
  });

  it("Replay posts {raw_response_id}", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({ raw_items: rawItems(true) });
    api.routes.set("POST /api/system/raw-items/replay", () => json({ operation_id: 31, items: 3 }, 202));
    renderApp("/settings#system");
    const raw = await block("raw_items");
    const row = item(raw, "Arbeitnow feed");
    expect(within(row).getByText("missing_title").tagName).toBe("CODE");
    fireEvent.click(within(row).getByRole("button", { name: en["attention.replay"] }));
    await waitFor(() => {
      expect(bodyOf(api, "POST /api/system/raw-items/replay")).toEqual({ raw_response_id: 7 });
    });
    expect(await screen.findByText("Replay queued (3 items)")).toBeTruthy();
  });

  it("a non-replayable response shows why", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({ raw_items: rawItems(false) });
    renderApp("/settings#system");
    const row = item(await block("raw_items"), "Arbeitnow feed");
    expect(within(row).queryByRole("button", { name: en["attention.replay"] })).toBeNull();
    expect(within(row).getByText(en["attention.not_replayable"])).toBeTruthy();
  });

  it("Resolve asks, naming the jobs, then posts the decision", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({
      identity_conflicts: {
        count: 1,
        items: [{ conflict_id: 5, job_ids: [21, 22], reason: "pub:stepstone.de", opened_at: "2026-11-03T04:00:00Z" }],
      },
    });
    api.routes.set("POST /api/identity-conflicts/5/resolve", () => json({ conflict_id: 5, status: "resolved_merged" }, 202));
    renderApp("/settings#system");
    const row = item(await block("identity_conflicts"), "pub:stepstone.de");

    // Keep apart: asked, then cancelled, sends nothing
    fireEvent.click(within(row).getByRole("button", { name: en["attention.separate"] }));
    const apart = screen.getByRole("alertdialog", { name: en["attention.separate_title"] });
    expect(apart.textContent).toContain("job 21 and job 22");
    fireEvent.click(within(apart).getByRole("button", { name: en["action.cancel"] }));
    expect(screen.queryByRole("alertdialog")).toBeNull();

    fireEvent.click(within(row).getByRole("button", { name: en["attention.merge"] }));
    const merge = screen.getByRole("alertdialog", { name: en["attention.merge_title"] });
    expect(merge.textContent).toContain("job 22");
    expect(merge.textContent).toContain("job 21");
    expect(requestsTo(api, "POST /api/identity-conflicts/5/resolve")).toHaveLength(0);
    fireEvent.click(within(merge).getByRole("button", { name: en["attention.merge_confirm"] }));
    await waitFor(() => {
      expect(bodyOf(api, "POST /api/identity-conflicts/5/resolve")).toEqual({ decision: "merge" });
    });
    expect(requestsTo(api, "POST /api/identity-conflicts/5/resolve")).toHaveLength(1);
  });

  it("Release states the amount and sends a keyed {confirm: true}; release is not posted before the confirm", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({ quarantined_batches: QUARANTINED });
    api.routes.set("POST /api/batches/3/release", () => json({ submission_id: 3, state: "released", reserved_usd: "0.840000" }, 202));
    renderApp("/settings#system");
    const row = item(await block("quarantined_batches"), /Batch 3/);
    fireEvent.click(within(row).getByRole("button", { name: en["attention.release"] }));
    const dialog = screen.getByRole("alertdialog", { name: en["attention.release_title"] });
    expect(dialog.textContent).toContain("$0.84 stays counted as ambiguous spend until it settles.");
    expect(requestsTo(api, "POST /api/batches/3/release")).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: en["attention.release_confirm"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/batches/3/release")).toHaveLength(1);
    });
    const [release] = requestsTo(api, "POST /api/batches/3/release");
    expect(JSON.parse(release.body ?? "")).toEqual({ confirm: true });
    expect(release.headers.get("Idempotency-Key")).toMatch(UUID);
  });

  it("an item's time never breaks inside: its day, month and time are joined by no-break spaces", async () => {
    // Task 36's visual pass: in a narrow item column "sent 3 Nov, 05:00" broke between "3" and "Nov"
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({
      quarantined_batches: QUARANTINED,
      mass_disappearances: {
        count: 1,
        items: [{ snapshot_id: 11, instance_label: "Beispiel GmbH", absent_count: 18, baseline_active_count: 20, taken_at: "2026-11-03T05:00:00Z" }],
      },
    });
    renderApp("/settings#system");
    const batch = item(await block("quarantined_batches"), /Batch 3/);
    // 04:00 UTC is 05:00 in Berlin
    expect(batch.textContent).toContain("sent 3 Nov, 05:00");
    expect(batch.textContent).not.toMatch(/3 Nov|Nov, 05/);
    const mass = item(await block("mass_disappearances"), "Beispiel GmbH");
    expect(mass.textContent).toContain("snapshot 3 Nov, 06:00");
  });

  it("a retrieve-failing item without releasable has no Release; a releasable one confirms its own amount first", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({ retrieve_failing: RETRIEVE_FAILING });
    api.routes.set("POST /api/batches/6/release", () => json({ submission_id: 6, state: "released", reserved_usd: "1.250000" }, 202));
    renderApp("/settings#system");
    const failing = await block("retrieve_failing");
    const early = item(failing, "msgbatch_early");
    expect(within(early).queryByRole("button", { name: en["attention.release"] })).toBeNull();
    // its expiry (06:00 in Berlin) plus the hour the route waits
    expect(within(early).getByText("Can be released from 3 Nov, 07:00")).toBeTruthy();

    const late = item(failing, "msgbatch_late");
    fireEvent.click(within(late).getByRole("button", { name: en["attention.release"] }));
    const dialog = screen.getByRole("alertdialog", { name: en["attention.release_title"] });
    expect(dialog.textContent).toContain("$1.25 stays counted as ambiguous spend");
    expect(dialog.textContent).not.toContain("$2.10");
    expect(requestsTo(api, "POST /api/batches/6/release")).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: en["attention.release_confirm"] }));
    await waitFor(() => {
      expect(bodyOf(api, "POST /api/batches/6/release")).toEqual({ confirm: true });
    });
    expect(requestsTo(api, "POST /api/batches/5/release")).toHaveLength(0);
  });

  it("Retry posts for a failed target and for an unscorable group's failed member, not for a filtered one", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({
      failed_targets: {
        count: 1,
        items: [{ target_id: 51, job_id: 31, title: "Data Engineer", state: "failed_transient", state_reason: "provider_overloaded", refusal_category: null }],
      },
      unscorable_groups: {
        count: 1,
        items: [
          {
            group_id: 9,
            unscorable_reason: "all_failed",
            members: [
              { job_id: 41, title: "Platform Engineer", target_id: 61, target_state: "failed_request" },
              { job_id: 42, title: "Platform Engineer (m/w/d)", target_id: 62, target_state: "filtered_out" },
            ],
          },
        ],
      },
    });
    api.routes.set("POST /api/targets/51/retry", () => json({ target_id: 51, state: "pending" }, 202));
    api.routes.set("POST /api/targets/61/retry", () => json({ target_id: 61, state: "pending" }, 202));
    renderApp("/settings#system");
    const failed = item(await block("failed_targets"), "Data Engineer");
    expect(within(failed).getByText("provider_overloaded").tagName).toBe("CODE");
    fireEvent.click(within(failed).getByRole("button", { name: en["attention.retry"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/targets/51/retry")).toHaveLength(1);
    });

    const group = await block("unscorable_groups");
    expect(within(group).getByText(en["group_unscorable.all_failed"], { exact: false })).toBeTruthy();
    expect(within(item(group, "Platform Engineer (m/w/d)")).queryByRole("button", { name: en["attention.retry"] })).toBeNull();
    fireEvent.click(within(item(group, "Platform Engineer")).getByRole("button", { name: en["attention.retry"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/targets/61/retry")).toHaveLength(1);
    });
    expect(requestsTo(api, "POST /api/targets/62/retry")).toHaveLength(0);
  });

  it("Confirm closure asks, then posts the acknowledgement of the snapshot", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({
      mass_disappearances: {
        count: 1,
        items: [{ snapshot_id: 11, instance_label: "Beispiel GmbH", absent_count: 18, baseline_active_count: 20, taken_at: "2026-11-03T05:00:00Z" }],
      },
    });
    api.routes.set("POST /api/ats-snapshots/11/acknowledge", () => json({ snapshot_id: 11, closed: 18 }));
    renderApp("/settings#system");
    const row = item(await block("mass_disappearances"), "Beispiel GmbH");
    expect(row.textContent).toContain("18 postings of 20 missing");
    fireEvent.click(within(row).getByRole("button", { name: en["attention.confirm_closure"] }));
    const dialog = screen.getByRole("alertdialog", { name: en["attention.closure_title"] });
    expect(dialog.textContent).toContain("Beispiel GmbH");
    expect(requestsTo(api, "POST /api/ats-snapshots/11/acknowledge")).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: en["attention.confirm_closure"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/ats-snapshots/11/acknowledge")).toHaveLength(1);
    });
    expect(await screen.findByText("18 postings closed")).toBeTruthy();
  });

  it("Ack posts the paused lane's acknowledgement", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({ paused_lane: { reason: "reservation_bound_violated", alert_id: 9 } });
    api.routes.set("POST /api/alerts/9/ack", () => json({ id: 9, acknowledged_at: "2026-11-03T07:00:00Z" }));
    renderApp("/settings#system");
    const paused = await block("paused_lane");
    // the reason mid-sentence, lowercased in English
    expect(paused.textContent).toContain("Scoring is paused: reservation bound violated");
    fireEvent.click(within(paused).getByRole("button", { name: en["attention.ack"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/alerts/9/ack")).toHaveLength(1);
    });
  });

  it("Retry apply posts", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({ scope_apply_failed: { version_id: 14, alert_id: 4 } });
    api.routes.set("POST /api/settings/scope/retry-apply", () => json({ operation_id: 77 }, 202));
    renderApp("/settings#system");
    const failed = await block("scope_apply_failed");
    expect(failed.textContent).toContain("14");
    fireEvent.click(within(failed).getByRole("button", { name: en["banner.retry_apply"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/settings/scope/retry-apply")).toHaveLength(1);
    });
  });

  it("an action refreshes needs attention and the status", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({ raw_items: rawItems(true) });
    api.routes.set("POST /api/system/raw-items/replay", () => {
      api.attention = attentionOut();
      return json({ operation_id: 31, items: 3 }, 202);
    });
    renderApp("/settings#system");
    const row = item(await block("raw_items"), "Arbeitnow feed");
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/system/status").length).toBeGreaterThan(0);
    });
    const attentionReads = requestsTo(api, "GET /api/system/needs-attention").length;
    const statusReads = requestsTo(api, "GET /api/system/status").length;
    fireEvent.click(within(row).getByRole("button", { name: en["attention.replay"] }));
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/system/needs-attention").length).toBe(attentionReads + 1);
    });
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/system/status").length).toBeGreaterThan(statusReads);
    });
    // the read again shows the response gone
    await waitFor(async () => {
      expect(within(await block("raw_items")).getByText(en["attention.nothing"])).toBeTruthy();
    });
  });

  it("an action's button is disabled while it runs, and a refusal shows at its item", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({
      failed_targets: {
        count: 2,
        items: [
          { target_id: 51, job_id: 31, title: "Data Engineer", state: "refused", state_reason: null, refusal_category: "policy" },
          { target_id: 52, job_id: 32, title: "Site Reliability Engineer", state: "failed_request", state_reason: null, refusal_category: null },
        ],
      },
    });
    let answer: (response: Response) => void = () => undefined;
    api.routes.set("POST /api/targets/51/retry", () => new Promise<Response>((resolve) => { answer = resolve; }));
    renderApp("/settings#system");
    const failed = await block("failed_targets");
    const first = item(failed, "Data Engineer");
    const retry = within(first).getByRole("button", { name: en["attention.retry"] });
    fireEvent.click(retry);
    await waitFor(() => {
      expect((retry as HTMLButtonElement).disabled).toBe(true);
    });
    answer(problem(409, "conflict_state", { title: "The target cannot be moved now" }));
    expect(await within(first).findByRole("alert")).toBeTruthy();
    expect(within(first).getByRole("alert").textContent).toContain(en["problem.conflict_state"]);
    expect((retry as HTMLButtonElement).disabled).toBe(false);
    expect(within(item(failed, "Site Reliability Engineer")).queryByRole("alert")).toBeNull();
  });

  it("the release and a retry carry an Idempotency-Key; no other action does", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({
      identity_conflicts: {
        count: 1,
        items: [{ conflict_id: 5, job_ids: [21, 22], reason: "pub:stepstone.de", opened_at: "2026-11-03T04:00:00Z" }],
      },
      mass_disappearances: {
        count: 1,
        items: [{ snapshot_id: 11, instance_label: "Beispiel GmbH", absent_count: 18, baseline_active_count: 20, taken_at: "2026-11-03T05:00:00Z" }],
      },
      raw_items: rawItems(true),
      quarantined_batches: QUARANTINED,
      failed_targets: {
        count: 1,
        items: [{ target_id: 51, job_id: 31, title: "Data Engineer", state: "failed_transient", state_reason: null, refusal_category: null }],
      },
      paused_lane: { reason: "auth_failed", alert_id: 9 },
      scope_apply_failed: { version_id: 14, alert_id: null },
    });
    api.routes.set("POST /api/system/raw-items/replay", () => json({ operation_id: 31, items: 3 }, 202));
    api.routes.set("POST /api/batches/3/release", () => json({ submission_id: 3, state: "released", reserved_usd: "0.840000" }, 202));
    api.routes.set("POST /api/targets/51/retry", () => json({ target_id: 51, state: "pending" }, 202));
    api.routes.set("POST /api/alerts/9/ack", () => json({ id: 9, acknowledged_at: "2026-11-03T07:00:00Z" }));
    api.routes.set("POST /api/settings/scope/retry-apply", () => json({ operation_id: 77 }, 202));
    api.routes.set("POST /api/identity-conflicts/5/resolve", () => json({ conflict_id: 5, status: "resolved_separate" }, 202));
    api.routes.set("POST /api/ats-snapshots/11/acknowledge", () => json({ snapshot_id: 11, closed: 18 }));
    renderApp("/settings#system");
    fireEvent.click(within(await block("identity_conflicts")).getByRole("button", { name: en["attention.separate"] }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: en["attention.separate_confirm"] }));
    fireEvent.click(within(await block("mass_disappearances")).getByRole("button", { name: en["attention.confirm_closure"] }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: en["attention.confirm_closure"] }));
    fireEvent.click(within(item(await block("raw_items"), "Arbeitnow feed")).getByRole("button", { name: en["attention.replay"] }));
    fireEvent.click(within(await block("quarantined_batches")).getByRole("button", { name: en["attention.release"] }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: en["attention.release_confirm"] }));
    fireEvent.click(within(await block("failed_targets")).getByRole("button", { name: en["attention.retry"] }));
    fireEvent.click(within(await block("paused_lane")).getByRole("button", { name: en["attention.ack"] }));
    fireEvent.click(within(await block("scope_apply_failed")).getByRole("button", { name: en["banner.retry_apply"] }));
    const posts = [
      "POST /api/identity-conflicts/5/resolve",
      "POST /api/ats-snapshots/11/acknowledge",
      "POST /api/system/raw-items/replay",
      "POST /api/batches/3/release",
      "POST /api/targets/51/retry",
      "POST /api/alerts/9/ack",
      "POST /api/settings/scope/retry-apply",
    ];
    await waitFor(() => {
      for (const call of posts) {
        expect(requestsTo(api, call), call).toHaveLength(1);
      }
    });
    const keyed = posts.filter((call) => requestsTo(api, call)[0]?.headers.get("Idempotency-Key") != null);
    expect(keyed).toEqual(["POST /api/batches/3/release", "POST /api/targets/51/retry"]);
  });

  it("a failed retry apply shows at its item, with no transient toast", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({ scope_apply_failed: { version_id: 14, alert_id: 4 } });
    api.routes.set("POST /api/settings/scope/retry-apply", () => problem(503, "internal", { title: "Service unavailable" }));
    renderApp("/settings#system");
    const failed = await block("scope_apply_failed");
    fireEvent.click(within(failed).getByRole("button", { name: en["banner.retry_apply"] }));
    expect(await within(failed).findByRole("alert")).toBeTruthy();
    expect(screen.queryByText(en["toast.transient"])).toBeNull();
  });

  it("reads needs attention again every 60 s in the background", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const api = fakeApi({ signedIn: true });
      renderApp("/settings#system");
      await block("raw_items");
      const reads = () => requestsTo(api, "GET /api/system/needs-attention");
      expect(reads()).toHaveLength(1);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(55_000);
      });
      expect(reads()).toHaveLength(1);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      await waitFor(() => {
        expect(reads()).toHaveLength(2);
      });
      for (const read of reads()) {
        expect(read.headers.get("X-JH-Background")).toBe("1");
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it("broken sources are information, a degraded one a warning; a job opens in the Jobs page's drawer", async () => {
    const api = fakeApi({ signedIn: true });
    api.attention = attentionOut({
      broken_sources: {
        count: 2,
        items: [
          { instance_id: 4, source_id: "greenhouse", label: "Beispiel GmbH", health: "breaker_open", severity: "critical", last_error_code: "http_503", breaker_opened_at: "2026-11-03T04:00:00Z", half_open_after: "2026-11-03T08:00:00Z" },
          { instance_id: 5, source_id: "lever", label: "Muster AG", health: "degraded", severity: "warn", last_error_code: null, breaker_opened_at: null, half_open_after: null },
        ],
      },
      failed_targets: {
        count: 1,
        items: [{ target_id: 51, job_id: 31, title: "Data Engineer", state: "failed_transient", state_reason: null, refusal_category: null }],
      },
    });
    const app = renderApp("/settings#system");
    const broken = await block("broken_sources");
    expect(within(broken).queryAllByRole("button")).toHaveLength(0);
    const critical = item(broken, "Beispiel GmbH");
    expect(critical.textContent).toContain(en["instance_health.breaker_open"]);
    expect(within(critical).getByText("http_503").tagName).toBe("CODE");
    const degraded = item(broken, "Muster AG");
    expect(within(degraded).getByText(en["alert_severity.warn"])).toBeTruthy();
    expect(degraded.textContent).toContain(en["instance_health.degraded"]);

    fireEvent.click(within(await block("failed_targets")).getByRole("button", { name: "Data Engineer" }));
    await waitFor(() => {
      expect(app.router.state.location.pathname).toBe("/");
    });
    expect(app.router.state.location.searchStr).toBe("?job=31");
  });
});
