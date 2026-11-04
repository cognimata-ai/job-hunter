import { createMemoryHistory } from "@tanstack/react-router";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App, type AppParts, createApp } from "../app/App";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import { type FakeApi, fakeApi, jobCard, jobsPage, json, pagedJobs, problem, requestsTo } from "../test/api";
import { NOW, renderApp, settled } from "../test/app";

type JobCardOut = components["schemas"]["JobCardOut"];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
});

afterEach(async () => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

function cards(ids: number[]): JobCardOut[] {
  return ids.map((id) => jobCard({ job_id: id, title: `Job ${String(id)}` }));
}

function list(): HTMLElement {
  return screen.getByRole("list", { name: en["jobs.title"] });
}

function articles(): HTMLElement[] {
  return within(list()).getAllByRole("article");
}

/** The app at `path`, its queries retrying without waiting (the same policy, without the 1-2-4 s waits). */
function renderQuick(path: string): AppParts {
  const app = createApp({ history: createMemoryHistory({ initialEntries: [path] }) });
  const queries = app.queryClient.getDefaultOptions().queries;
  app.queryClient.setDefaultOptions({ queries: { ...queries, retryDelay: 0 } });
  render(<App app={app} />);
  return app;
}

function hiddenRow(jobId: number) {
  const at = NOW.toISOString();
  return { job: { job_id: jobId, seen_at: null, saved_at: null, applied_at: null, hidden_at: at, note: null, updated_at: at }, members: [] };
}

describe("JobsPage", () => {
  it("restarts from page 1 on invalid_cursor", async () => {
    const api = fakeApi({ signedIn: true });
    const paged = pagedJobs([cards([1, 2]), cards([3])]);
    let lost = true; // the api has restarted with a new cursor key: the old cursors are refused once
    api.routes.set("GET /api/jobs", (request) => {
      if (request.search.has("cursor") && lost) {
        lost = false;
        return problem(400, "invalid_cursor");
      }
      return paged.route(request);
    });
    renderApp("/?sort=posted");
    await screen.findByRole("article", { name: "Job 2" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: en["jobs.load_more"] }));
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs")).toHaveLength(3);
    });
    const [first, refused, again] = requestsTo(api, "GET /api/jobs");
    expect(first.search.has("cursor")).toBe(false);
    expect(refused.search.get("cursor")).toBe("c1.g1");
    expect(again.search.has("cursor")).toBe(false); // page 1 again, under the same filters
    expect(again.search.get("sort")).toBe("posted");
    expect(await screen.findByRole("article", { name: "Job 2" })).toBeTruthy();
    expect(screen.getByRole("button", { name: en["jobs.load_more"] })).toBeTruthy();
    await settled();
    expect(screen.queryByRole("alert")).toBeNull(); // silently: no problem box
    expect(screen.queryByText(en["problem.invalid_cursor"])).toBeNull(); // and no toast
    expect(screen.queryByText(en["toast.transient"])).toBeNull();
    await user.click(screen.getByRole("button", { name: en["jobs.load_more"] }));
    expect(await screen.findByRole("article", { name: "Job 3" })).toBeTruthy();
  });

  it("counts and the 10,000+ form", async () => {
    const api = fakeApi({ signedIn: true });
    api.jobs = { ...jobsPage(cards([1])), counts: { total_estimate: 1284, unscored: 212, capped: false } };
    renderApp("/");
    expect(await screen.findByText("1,284 jobs · 212 unscored")).toBeTruthy();
    cleanupAndRender(api, { total_estimate: 1, unscored: 0, capped: false }, "en");
    expect(await screen.findByText("1 job · 0 unscored")).toBeTruthy();
    cleanupAndRender(api, { total_estimate: 10000, unscored: 340, capped: true }, "en");
    expect(await screen.findByText("10,000+ jobs · 340 unscored")).toBeTruthy();
    cleanupAndRender(api, { total_estimate: 10000, unscored: 10000, capped: true }, "de");
    expect(await screen.findByText("10.000+ Stellen · 10.000+ unbewertet")).toBeTruthy();
  });

  it("j and k move focus and Enter opens the drawer param", async () => {
    const api = fakeApi({ signedIn: true });
    api.jobs = jobsPage(cards([4, 7, 9]));
    const app = renderApp("/?sort=posted");
    await screen.findByRole("article", { name: "Job 9" });
    const [first, second, third] = articles();
    expect([first.tabIndex, second.tabIndex, third.tabIndex]).toEqual([0, -1, -1]);
    const user = userEvent.setup();
    await user.keyboard("j"); // from outside the list: to its current card
    expect(document.activeElement).toBe(first);
    await user.keyboard("j");
    expect(document.activeElement).toBe(second);
    expect([first.tabIndex, second.tabIndex, third.tabIndex]).toEqual([-1, 0, -1]); // the roving tabIndex follows
    await user.keyboard("jj"); // the last card stays
    expect(document.activeElement).toBe(third);
    await user.keyboard("k");
    expect(document.activeElement).toBe(second);
    const before = app.router.history.length;
    await user.keyboard("{Enter}");
    await waitFor(() => {
      expect(app.router.state.location.search).toMatchObject({ job: "7", sort: "posted" });
    });
    expect(app.router.history.length).toBe(before + 1); // a history entry: Back closes the drawer
    expect(document.activeElement).toBe(second);
    // a held modifier is the browser's or another shortcut's
    await user.keyboard("{Control>}j{/Control}");
    expect(document.activeElement).toBe(second);
    // a key inside a dialog is the dialog's
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    const button = document.createElement("button");
    dialog.append(button);
    document.body.append(dialog);
    button.focus();
    await user.keyboard("j");
    expect(document.activeElement).toBe(button);
    dialog.remove();
  });

  it("j and k are ignored in the search box", async () => {
    const api = fakeApi({ signedIn: true });
    api.jobs = jobsPage(cards([4, 7]));
    renderApp("/");
    await screen.findByRole("article", { name: "Job 7" });
    const user = userEvent.setup();
    const box = screen.getByRole("searchbox", { name: en["filters.search"] });
    await user.click(box);
    await user.keyboard("jkj");
    expect(document.activeElement).toBe(box);
    expect((box as HTMLInputElement).value).toBe("jkj");
    const confidence = screen.getByRole("combobox", { name: en["filters.confidence"] });
    confidence.focus();
    await user.keyboard("k");
    expect(document.activeElement).toBe(confidence);
    expect(articles().map((card) => card.tabIndex)).toEqual([0, -1]);
  });

  it("empty, loading, error and no-profile states", async () => {
    const api = fakeApi({ signedIn: true });
    let release: (() => void) | null = null;
    api.routes.set("GET /api/jobs", () =>
      new Promise<Response>((resolve) => {
        release = () => { resolve(json(jobsPage(cards([1, 2])))); };
      }),
    );
    renderApp("/");
    await screen.findByRole("heading", { level: 1, name: en["jobs.title"] });
    const loading = await within(screen.getByRole("main")).findByRole("status", { busy: true });
    expect(loading.querySelectorAll("[aria-hidden=true]")).toHaveLength(8);
    await act(async () => {
      release?.();
      await Promise.resolve();
    });
    expect(await screen.findByRole("article", { name: "Job 2" })).toBeTruthy();
    expect(requestsTo(api, "GET /api/jobs")[0].search.toString()).toBe("limit=50"); // the server's defaults
    expect(screen.queryByText(en["jobs.no_profile"])).toBeNull(); // a profile is active
    cleanup();

    // a failed first read: the problem with Retry
    api.routes.set("GET /api/jobs", () => problem(422, "validation_failed"));
    renderApp("/");
    const box = await screen.findByRole("alert");
    expect(box.textContent).toContain(en["problem.validation_failed"]);
    api.routes.delete("GET /api/jobs");
    api.jobs = jobsPage(cards([1]));
    await userEvent.setup().click(within(box).getByRole("button", { name: en["action.retry"] }));
    expect(await screen.findByRole("article", { name: "Job 1" })).toBeTruthy();
    cleanup();

    // no cards under the defaults: nothing collected yet
    api.jobs = jobsPage([]);
    renderApp("/");
    expect(await screen.findByText(en["jobs.empty_title"])).toBeTruthy();
    expect(screen.queryByRole("button", { name: en["jobs.reset_filters"] })).toBeNull();
    cleanup();

    // no cards under filters: Reset filters
    const app = renderApp("/?q=python&work_mode=remote&label=1");
    expect(await screen.findByText(en["jobs.no_match"])).toBeTruthy();
    await userEvent.setup().click(screen.getByRole("button", { name: en["jobs.reset_filters"] }));
    await waitFor(() => {
      expect(app.router.state.location.searchStr).toBe("?label=1");
    });
    expect(await screen.findByText(en["jobs.empty_title"])).toBeTruthy();
    cleanup();

    // no active profile: the notice above the unscored cards
    api.profileActive = false;
    api.jobs = jobsPage(cards([1, 2]));
    renderApp("/");
    const notice = await screen.findByRole("link", { name: en["jobs.no_profile"] });
    expect(notice.getAttribute("href")).toBe("/settings#cv");
    expect(articles()).toHaveLength(2);
    expect(notice.compareDocumentPosition(list()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy(); // above the list
  });

  it("a load-more error keeps the loaded cards", async () => {
    const api = fakeApi({ signedIn: true });
    const paged = pagedJobs([cards([1, 2]), cards([3])]);
    let down = true;
    api.routes.set("GET /api/jobs", (request) =>
      request.search.has("cursor") && down ? problem(503, "internal") : paged.route(request),
    );
    renderQuick("/");
    await screen.findByRole("article", { name: "Job 2" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: en["jobs.load_more"] }));
    const box = await screen.findByRole("alert");
    expect(box.textContent).toContain(en["problem.internal"]);
    expect(articles()).toHaveLength(2); // the loaded cards stay
    expect(list().compareDocumentPosition(box) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy(); // below the list
    expect(requestsTo(api, "GET /api/jobs").filter((r) => r.search.has("cursor"))).toHaveLength(4); // retried 3 times
    down = false;
    await user.click(within(box).getByRole("button", { name: en["action.retry"] }));
    expect(await screen.findByRole("article", { name: "Job 3" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(articles()).toHaveLength(3);
  });

  it("Hide the focused card by keyboard, and focus lands on the next card", async () => {
    const api = fakeApi({ signedIn: true });
    const paged = pagedJobs([cards([1, 2, 3])]);
    api.routes.set("GET /api/jobs", paged.route);
    for (const id of [1, 2, 3]) {
      api.routes.set(`PUT /api/jobs/${String(id)}/actions`, () => {
        paged.pages = [paged.pages[0].filter((card) => card.job_id !== id)]; // the default list leaves it out
        return json(hiddenRow(id));
      });
    }
    renderApp("/");
    await screen.findByRole("article", { name: "Job 3" });
    const user = userEvent.setup();
    const hideByKeyboard = async (card: HTMLElement) => {
      await user.keyboard("j");
      for (let i = 0; i < 5 && document.activeElement !== card; i++) {
        await user.keyboard("j"); // to the card
      }
      expect(document.activeElement).toBe(card);
      for (let i = 0; i < 8 && document.activeElement?.textContent !== en["card.hide"]; i++) {
        await user.tab(); // to its Hide button
      }
      expect(card.contains(document.activeElement)).toBe(true);
      await user.keyboard("{Enter}");
    };
    await hideByKeyboard(screen.getByRole("article", { name: "Job 1" }));
    await waitFor(() => {
      expect(screen.queryByRole("article", { name: "Job 1" })).toBeNull();
    });
    expect(document.activeElement).toBe(screen.getByRole("article", { name: "Job 2" })); // the next card
    expect(screen.getByRole("article", { name: "Job 2" }).tabIndex).toBe(0);
    await hideByKeyboard(screen.getByRole("article", { name: "Job 3" }));
    await waitFor(() => {
      expect(screen.queryByRole("article", { name: "Job 3" })).toBeNull();
    });
    expect(document.activeElement).toBe(screen.getByRole("article", { name: "Job 2" })); // the last: the previous one
    await hideByKeyboard(screen.getByRole("article", { name: "Job 2" }));
    await waitFor(() => {
      expect(screen.queryByRole("article")).toBeNull();
    });
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: en["jobs.title"] })); // none left
  });

  it("the sort select writes the sort and keeps the filters", async () => {
    const api = fakeApi({ signedIn: true });
    api.jobs = jobsPage(cards([1]));
    const app = renderApp("/?q=python");
    await screen.findByRole("article", { name: "Job 1" });
    const select = screen.getByRole("combobox", { name: en["jobs.sort"] });
    expect([...(select as HTMLSelectElement).options].map((o) => o.textContent)).toEqual([
      en["jobs.sort.score_mid"],
      en["jobs.sort.score_low"],
      en["jobs.sort.posted"],
      en["jobs.sort.first_seen"],
    ]);
    await userEvent.setup().selectOptions(select, en["jobs.sort.first_seen"]);
    await waitFor(() => {
      expect(app.router.state.location.search).toEqual({ q: "python", sort: "first_seen" });
    });
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs").at(-1)?.search.get("sort")).toBe("first_seen");
    });
  });
});

function cleanupAndRender(api: FakeApi, counts: components["schemas"]["CountsOut"], language: "en" | "de"): void {
  cleanup();
  api.uiLanguage = language;
  api.jobs = { ...jobsPage(cards([1])), counts };
  renderApp("/");
}
