import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { fakeApi, historyItem, json, problem, requestsTo } from "../test/api";
import { NOW, renderInProviders, settled } from "../test/app";
import { HistoryTab } from "./HistoryTab";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const OLDER = historyItem({
  result_id: 20,
  published_at: "2026-10-20T09:00:00Z",
  profile_version_no: 3,
  status: "contested",
  instances: [
    { det_result_id: 40, scope_version_id: 2, is_current: false, mid: "60.00", low: "50.00", high: "70.00", coverage: "0.600", confidence: "medium", computed_at: "2026-10-20T09:00:00Z" },
    { det_result_id: 39, scope_version_id: 1, is_current: false, mid: null, low: null, high: null, coverage: null, confidence: null, computed_at: "2026-10-19T09:00:00Z" },
  ],
});

function results(): HTMLElement[] {
  return within(screen.getByRole("list", { name: en["history.title"] })).getAllByRole("article");
}

describe("HistoryTab", () => {
  it("shows each result with its instances, newest first as served", () => {
    fakeApi({ signedIn: true });
    renderInProviders(<HistoryTab jobId={5} first={{ items: [historyItem(), OLDER], next_cursor: null }} />);
    const [current, older] = results();
    expect(within(current).getByRole("heading").textContent).toBe("2 Nov 2026, 10:00 · profile v4 · rubric v1 · tuple 2 · accepted");
    const [instance] = within(current).getAllByRole("listitem");
    expect(instance.textContent).toBe(`scope v3 · M 82 · L 74 · U 90 · coverage 85% · confidence high · 2 Nov 2026, 10:00 · ${en["history.current"]}`);
    expect(within(older).getByRole("heading").textContent).toContain(en["result_status.contested"]);
    const [, verified] = within(older).getAllByRole("listitem");
    expect(verified.textContent).toBe("scope v1 · M — · L — · U — · coverage — · confidence — · 19 Oct 2026, 11:00");
    expect(screen.queryByRole("button", { name: en["jobs.load_more"] })).toBeNull();
  });

  it("without results says so", () => {
    fakeApi({ signedIn: true });
    renderInProviders(<HistoryTab jobId={5} first={{ items: [], next_cursor: null }} />);
    expect(screen.getByText(en["history.none"])).toBeTruthy();
  });

  it("Load more reads the next page from the cursor", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/jobs/5/history", (request) =>
      request.search.get("cursor") === "h1" ? json({ items: [OLDER], next_cursor: null }) : problem(400, "invalid_cursor"),
    );
    renderInProviders(<HistoryTab jobId={5} first={{ items: [historyItem()], next_cursor: "h1" }} />);
    await settled();
    expect(requestsTo(api, "GET /api/jobs/5/history")).toHaveLength(0); // the first page is the detail's
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: en["jobs.load_more"] }));
    await waitFor(() => {
      expect(results()).toHaveLength(2);
    });
    expect(screen.queryByRole("button", { name: en["jobs.load_more"] })).toBeNull();
  });

  it("the history load-more error keeps the loaded items", async () => {
    const api = fakeApi({ signedIn: true });
    let fail = true;
    api.routes.set("GET /api/jobs/5/history", () => (fail ? problem(404, "not_found") : json({ items: [OLDER], next_cursor: null })));
    renderInProviders(<HistoryTab jobId={5} first={{ items: [historyItem()], next_cursor: "h1" }} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: en["jobs.load_more"] }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(en["problem.not_found"]);
    expect(results()).toHaveLength(1); // the loaded result stays
    fail = false;
    await user.click(within(alert).getByRole("button", { name: en["action.retry"] }));
    await waitFor(() => {
      expect(results()).toHaveLength(2);
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a refused cursor starts again from the first page, silently", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/jobs/5/history", (request) =>
      request.search.has("cursor") ? problem(400, "invalid_cursor") : json({ items: [historyItem()], next_cursor: "h2" }),
    );
    renderInProviders(<HistoryTab jobId={5} first={{ items: [historyItem()], next_cursor: "h1" }} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: en["jobs.load_more"] }));
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs/5/history").map((r) => r.search.get("cursor"))).toEqual(["h1", null]);
    });
    await settled();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(results()).toHaveLength(1);
    expect(screen.getByRole("button", { name: en["jobs.load_more"] })).toBeTruthy();
  });
});
