import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import { type FakeApi, fakeApi, jobCard, json, pagedJobs, requestsTo } from "../test/api";
import { NOW, renderApp, settled } from "../test/app";

type JobCardOut = components["schemas"]["JobCardOut"];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function cards(ids: number[]): JobCardOut[] {
  return ids.map((id) => jobCard({ job_id: id, title: `Job ${String(id)}` }));
}

function titles(): string[] {
  const list = screen.getByRole("list", { name: en["jobs.title"] });
  return within(list).getAllByRole("article").map((card) => within(card).getByRole("heading").textContent);
}

/** The list reads, as `cursor` (or `-` for a first page) and `limit`. */
function reads(api: FakeApi): string[] {
  return requestsTo(api, "GET /api/jobs").map((r) => `${r.search.get("cursor") ?? "-"} ${r.search.get("limit") ?? "?"}`);
}

describe("useJobsList", () => {
  it("pages with next_cursor", async () => {
    const api = fakeApi({ signedIn: true });
    const list = pagedJobs([cards([1, 2]), cards([3, 4]), cards([5])]);
    api.routes.set("GET /api/jobs", list.route);
    renderApp("/?sort=posted&work_mode=remote&work_mode=hybrid");
    expect(await screen.findByRole("article", { name: "Job 2" })).toBeTruthy();
    expect(titles()).toEqual(["Job 1", "Job 2"]);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: en["jobs.load_more"] }));
    expect(await screen.findByRole("article", { name: "Job 4" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: en["jobs.load_more"] }));
    expect(await screen.findByRole("article", { name: "Job 5" })).toBeTruthy();
    expect(titles()).toEqual(["Job 1", "Job 2", "Job 3", "Job 4", "Job 5"]);
    expect(screen.queryByRole("button", { name: en["jobs.load_more"] })).toBeNull(); // no next cursor
    expect(reads(api)).toEqual(["- 50", "c1.g1 50", "c2.g1 50"]);
    for (const request of requestsTo(api, "GET /api/jobs")) {
      expect(request.search.get("sort")).toBe("posted"); // every page under the same filters
      expect(request.search.getAll("work_mode")).toEqual(["hybrid", "remote"]);
    }
  });

  it("ranking_changed sets the flag and refresh reloads page 1", async () => {
    const api = fakeApi({ signedIn: true });
    const list = pagedJobs([cards([1, 2]), cards([3, 4]), cards([5])]);
    api.routes.set("GET /api/jobs", list.route);
    renderApp("/");
    await screen.findByRole("article", { name: "Job 2" });
    const status = screen.getByRole("status", { name: en["jobs.ranking_status"] });
    expect(status.textContent).toBe(""); // the region is there before it says anything
    list.generation = 2; // a write elsewhere (another tab, a scoring run) moves the ranking
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: en["jobs.load_more"] }));
    const refresh = await within(status).findByRole("button", { name: en["jobs.ranking_changed"] });
    expect(titles()).toEqual(["Job 1", "Job 2", "Job 3", "Job 4"]); // nothing moves under the reader
    list.pages = [cards([4, 1]), cards([2, 3]), cards([5])];
    await user.click(refresh);
    await waitFor(() => {
      expect(titles()).toEqual(["Job 4", "Job 1"]); // page 1 alone, read afresh
    });
    expect(within(status).queryByRole("button")).toBeNull();
    expect(reads(api)).toEqual(["- 50", "c1.g1 50", "- 50"]);
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: en["jobs.title"] }));
    await user.click(screen.getByRole("button", { name: en["jobs.load_more"] }));
    expect(await screen.findByRole("article", { name: "Job 3" })).toBeTruthy();
    expect(reads(api).at(-1)).toBe("c1.g2 50"); // the new first page's generation
    expect(within(status).queryByRole("button")).toBeNull();
  });

  it("an own Save refetch raises no ranking flag", async () => {
    const api = fakeApi({ signedIn: true });
    const list = pagedJobs([cards([1, 2]), cards([3, 4])]);
    api.routes.set("GET /api/jobs", list.route);
    api.routes.set("PUT /api/jobs/3/actions", () => {
      list.generation += 1; // the write moves the ranking generation, as the api's does
      return json({ job: { job_id: 3, seen_at: null, saved_at: NOW.toISOString(), applied_at: null, hidden_at: null, note: null, updated_at: NOW.toISOString() }, members: [] });
    });
    renderApp("/");
    await screen.findByRole("article", { name: "Job 2" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: en["jobs.load_more"] }));
    const card = await screen.findByRole("article", { name: "Job 3" });
    await user.click(within(card).getByRole("button", { name: en["card.save"] }));
    // the loaded pages are read again in a chain, the second from the fresh first page's cursor
    await waitFor(() => {
      expect(reads(api)).toEqual(["- 50", "c1.g1 50", "- 50", "c1.g2 50"]);
    });
    await settled();
    expect(within(screen.getByRole("status", { name: en["jobs.ranking_status"] })).queryByRole("button")).toBeNull();
    expect(titles()).toEqual(["Job 1", "Job 2", "Job 3", "Job 4"]);
  });

  it("is not read again on focus or reconnect, even once stale", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/jobs", pagedJobs([cards([1])]).route);
    renderApp("/");
    await screen.findByRole("article", { name: "Job 1" });
    vi.setSystemTime(NOW.getTime() + 60 * 60_000); // an hour later: stale by any freshness
    window.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("offline"));
    window.dispatchEvent(new Event("online"));
    await settled();
    expect(reads(api)).toEqual(["- 50"]);
    expect(requestsTo(api, "GET /api/auth/session").length).toBeGreaterThan(1); // the other reads did refetch
  });
});
