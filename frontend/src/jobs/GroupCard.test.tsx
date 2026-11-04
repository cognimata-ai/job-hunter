import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App, createApp } from "../app/App";
import de from "../i18n/de.json";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import {
  type FakeApi,
  fakeApi,
  groupCard,
  groupOut,
  jobCard,
  jobDetail,
  jobsPage,
  json,
  problem,
  representativeDetail,
  requestsTo,
  userActions,
} from "../test/api";
import { NOW, renderApp, settled } from "../test/app";

const REPRESENTATIVE = "Backend Engineer";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
});

afterEach(async () => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

/** The api of a list holding the group card of job 9 (`groupCard()`) and the representative's detail. */
function stack(): FakeApi {
  const api = fakeApi({ signedIn: true });
  api.jobs = jobsPage([groupCard()]);
  api.routes.set("GET /api/jobs/9", () => json(representativeDetail()));
  return api;
}

async function groupCardOf(name = REPRESENTATIVE): Promise<HTMLElement> {
  const list = await screen.findByRole("list", { name: en["jobs.title"] });
  return within(list).getByRole("article", { name });
}

function bodies(api: FakeApi, call: string): unknown[] {
  return requestsTo(api, call).map((r) => JSON.parse(r.body ?? "null") as unknown);
}

describe("GroupCard", () => {
  it("header text", async () => {
    stack();
    renderApp("/");
    const card = await groupCardOf();
    // "Possible duplicates (3) · scored via JSearch and Greenhouse / Backend Engineer · applies to 3 members"
    expect(
      within(card).getByText(
        `${en["group.possible_duplicates"].replace("{{size}}", "3")} · ` +
          `${en["group.scored_via"].replace("{{sources}}", "JSearch and Greenhouse").replace("{{title}}", REPRESENTATIVE)} · ` +
          en["group.applies_other"].replace("{{count}}", "3"),
      ),
    ).toBeTruthy();
    // the representative's own card: its title, its score and its actions
    expect(within(card).getByRole("heading", { name: REPRESENTATIVE })).toBeTruthy();
    expect(within(card).getByRole("img", { name: "Score 82, range 74 to 90" })).toBeTruthy();
    expect(within(card).getByRole("button", { name: en["card.hide"] })).toBeTruthy();
    expect(screen.getAllByRole("article")).toHaveLength(1); // one card for the group
  });

  it("the header pluralises in de", async () => {
    const api = stack();
    api.uiLanguage = "de";
    renderApp("/");
    const list = await screen.findByRole("list", { name: de["jobs.title"] });
    const card = within(list).getByRole("article", { name: REPRESENTATIVE });
    // "Mögliche Duplikate (3) · bewertet über JSearch und Greenhouse / Backend Engineer · gilt für 3 Mitglieder"
    expect(
      within(card).getByText(
        `${de["group.possible_duplicates"].replace("{{size}}", "3")} · ` +
          `${de["group.scored_via"].replace("{{sources}}", "JSearch und Greenhouse").replace("{{title}}", REPRESENTATIVE)} · ` +
          de["group.applies_other"].replace("{{count}}", "3"),
      ),
    ).toBeTruthy();
    expect(de["group.applies_other"]).not.toBe(de["group.applies_one"]); // German agrees with the count too
  });

  it("lists deferred members", async () => {
    stack();
    renderApp("/");
    const card = await groupCardOf();
    const deferred = within(card).getByRole("list", { name: en["group.deferred"] });
    const items = within(deferred).getAllByRole("listitem");
    expect(items).toHaveLength(2); // the deferred members only, not the representative
    const [first, second] = items.map((item) => item.textContent);
    for (const part of ["Senior Backend Engineer", "Adzuna", en["filters.availability.active"], en["target_state.deferred_duplicate"], en["completeness.partial"]]) {
      expect(first).toContain(part);
    }
    for (const part of ["Backend Engineer (m/w/d)", "Arbeitnow", en["filters.availability.stale"], en["target_state.deferred_duplicate"], en["completeness.full"]]) {
      expect(second).toContain(part);
    }
    // the members' detail is read only when expanded
    const show = within(card).getByRole("button", { name: en["group.show_members"].replace("{{size}}", "3") });
    expect(show.getAttribute("aria-expanded")).toBe("false");
  });

  it("Hide applies to the group", async () => {
    const api = stack();
    api.routes.set("PUT /api/jobs/9/actions", () =>
      json({ job: userActions(9, { hidden_at: NOW.toISOString() }), members: [userActions(5, { hidden_at: NOW.toISOString() })] }),
    );
    renderApp("/");
    const card = await groupCardOf();
    const reads = requestsTo(api, "GET /api/jobs").length;
    await userEvent.setup().click(within(card).getByRole("button", { name: en["card.hide"] }));
    await waitFor(() => {
      expect(bodies(api, "PUT /api/jobs/9/actions")).toEqual([{ hidden: true, apply_to_group: true }]);
    });
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs").length).toBe(reads + 1);
    });
  });

  it("expanding reads the representative's detail once and shares the drawer cache", async () => {
    const api = stack();
    renderApp("/");
    const card = await groupCardOf();
    const user = userEvent.setup();
    const show = within(card).getByRole("button", { name: en["group.show_members"].replace("{{size}}", "3") });
    expect(requestsTo(api, "GET /api/jobs/9")).toHaveLength(0);
    await user.click(show);
    expect(show.getAttribute("aria-expanded")).toBe("true");
    const region = document.getElementById(show.getAttribute("aria-controls") ?? "");
    expect(region).not.toBeNull();
    const members = await within(region as HTMLElement).findByRole("list", { name: en["group.members"] });
    expect(within(members).getAllByRole("listitem")).toHaveLength(3);
    // collapsed and expanded again: nothing read again
    await user.click(within(card).getByRole("button", { name: en["group.hide_members"] }));
    expect(show.getAttribute("aria-expanded")).toBe("false");
    expect((region as HTMLElement).hidden).toBe(true);
    await user.click(within(card).getByRole("button", { name: en["group.show_members"].replace("{{size}}", "3") }));
    expect((region as HTMLElement).hidden).toBe(false);
    await settled();
    expect(requestsTo(api, "GET /api/jobs/9")).toHaveLength(1);
    // the drawer opens on the representative from the same cache entry: its detail is shown at once, while its
    // own read is still unanswered
    let answer: (response: Response) => void = () => undefined;
    api.routes.set("GET /api/jobs/9", () => new Promise<Response>((resolve) => { answer = resolve; }));
    await user.click(within(card).getByRole("link", { name: REPRESENTATIVE }));
    const dialog = await screen.findByRole("dialog", { name: REPRESENTATIVE });
    expect(within(dialog).getByRole("tab", { name: en["drawer.tab.duplicates"] })).toBeTruthy();
    expect(requestsTo(api, "GET /api/jobs/9")).toHaveLength(2); // its read in the background, still held
    answer(json(representativeDetail()));
    await settled();
  });

  it("a group gone meanwhile says so and reads the list again", async () => {
    const api = stack();
    api.routes.set("GET /api/jobs/9", () => json(representativeDetail(null)));
    renderApp("/");
    const card = await groupCardOf();
    const reads = requestsTo(api, "GET /api/jobs").length;
    await userEvent.setup().click(within(card).getByRole("button", { name: en["group.show_members"].replace("{{size}}", "3") }));
    expect(await within(card).findByText(en["group.gone"])).toBeTruthy();
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs").length).toBe(reads + 1);
    });
    await settled();
    expect(requestsTo(api, "GET /api/jobs").length).toBe(reads + 1); // once, not in a loop
  });

  it("a failed read of the members offers Retry", async () => {
    const api = stack();
    let hold: (response: Response) => void = () => undefined;
    let healthy = false;
    api.routes.set("GET /api/jobs/9", () =>
      healthy
        ? json(representativeDetail())
        : requestsTo(api, "GET /api/jobs/9").length === 1
          ? new Promise<Response>((resolve) => { hold = resolve; })
          : problem(500, "internal"),
    );
    // queries retry a 5xx three times: here without the 1-2-4 s waits
    const app = createApp({ history: createMemoryHistory({ initialEntries: ["/"] }) });
    app.queryClient.setDefaultOptions({ queries: { ...app.queryClient.getDefaultOptions().queries, retryDelay: 0 } });
    render(<App app={app} />);
    const card = await groupCardOf();
    const user = userEvent.setup();
    await user.click(within(card).getByRole("button", { name: en["group.show_members"].replace("{{size}}", "3") }));
    const loading = within(card).getByRole("status");
    expect(loading.getAttribute("aria-busy")).toBe("true"); // the skeleton while the detail is read
    hold(problem(500, "internal"));
    const box = await within(card).findByRole("alert");
    expect(box.textContent).toContain(en["problem.internal"]);
    expect(within(card).queryByRole("list", { name: en["group.members"] })).toBeNull();
    healthy = true;
    await user.click(within(box).getByRole("button", { name: en["action.retry"] }));
    const members = await within(card).findByRole("list", { name: en["group.members"] });
    expect(within(members).getAllByRole("listitem")).toHaveLength(3);
    expect(within(card).queryByRole("alert")).toBeNull();
  });

  it("a member's title pushes its job, and Back closes the drawer", async () => {
    const api = stack();
    api.routes.set("GET /api/jobs/5", () => json(jobDetail({ group: groupOut(), user: userActions(5, { seen_at: NOW.toISOString() }) })));
    const app = renderApp("/?sort=posted");
    const card = await groupCardOf();
    const user = userEvent.setup();
    await user.click(within(card).getByRole("button", { name: en["group.show_members"].replace("{{size}}", "3") }));
    const members = await within(card).findByRole("list", { name: en["group.members"] });
    const before = app.router.history.length;
    await user.click(within(members).getByRole("button", { name: "Senior Backend Engineer" }));
    expect(await screen.findByRole("dialog", { name: "Senior Backend Engineer" })).toBeTruthy();
    expect(app.router.state.location.search).toEqual({ sort: "posted", job: "5" });
    expect(app.router.history.length).toBe(before + 1); // pushed, as a card's own title does
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(app.router.state.location.search).toEqual({ sort: "posted" });
    expect(app.router.history.canGoBack()).toBe(false); // closed by Back: no entry left behind
  });

  it("j and k treat the group card as one card", async () => {
    const api = stack();
    api.jobs = jobsPage([jobCard({ job_id: 1, title: "Job 1" }), groupCard(), jobCard({ job_id: 2, title: "Job 2" })]);
    renderApp("/");
    const card = await groupCardOf();
    const first = screen.getByRole("article", { name: "Job 1" });
    first.focus();
    const user = userEvent.setup();
    await user.keyboard("j");
    expect(document.activeElement).toBe(card);
    expect(card.tabIndex).toBe(0); // the group card is the roving card
    await user.keyboard("j");
    expect(document.activeElement).toBe(screen.getByRole("article", { name: "Job 2" }));
    // from a control inside the group card (its disclosure), k moves to the card before it
    within(card).getByRole("button", { name: en["group.show_members"].replace("{{size}}", "3") }).focus();
    await user.keyboard("k");
    expect(document.activeElement).toBe(first);
  });
});
