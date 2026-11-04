import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App, createApp } from "../app/App";
import type { components } from "../api/schema";
import de from "../i18n/de.json";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import {
  DESCRIPTION,
  detailScore,
  type FakeApi,
  fakeApi,
  jobCard,
  jobDetail,
  jobsPage,
  json,
  problem,
  requestsTo,
  userActions,
} from "../test/api";
import { NOW, renderApp, settled } from "../test/app";

type JobDetailOut = components["schemas"]["JobDetailOut"];

const REL = "noopener noreferrer nofollow";
const TITLE = "Senior Backend Engineer";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
});

afterEach(async () => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

/** The api of a list holding job 5's card, its detail and the answer of its actions (the row after the write). */
function stack(detail: JobDetailOut = jobDetail()): FakeApi {
  const api = fakeApi({ signedIn: true });
  api.jobs = jobsPage([jobCard()]);
  api.routes.set("GET /api/jobs/5", () => json(detail));
  api.routes.set("PUT /api/jobs/5/actions", (request) => {
    const body = JSON.parse(request.body ?? "{}") as Record<string, unknown>;
    const at = NOW.toISOString();
    const seen = body.seen_on_open === true || body.seen === true ? at : null;
    return json({ job: userActions(5, { seen_at: seen, saved_at: body.saved === true ? at : null }), members: [] });
  });
  return api;
}

function bodies(api: FakeApi, call: string): unknown[] {
  return requestsTo(api, call).map((r) => JSON.parse(r.body ?? "null") as unknown);
}

/** The list at `/`, its card focused and opened with Enter. */
async function openFromCard() {
  const app = renderApp("/");
  const card = await screen.findByRole("article", { name: TITLE });
  card.focus();
  const user = userEvent.setup();
  await user.keyboard("{Enter}");
  const dialog = await screen.findByRole("dialog", { name: TITLE });
  return { app, card, dialog, user };
}

async function openPasted(path = "/?job=5") {
  const app = renderApp(path);
  const dialog = await screen.findByRole("dialog", { name: TITLE });
  return { app, dialog, user: userEvent.setup() };
}

describe("DetailDrawer: opening and closing", () => {
  it("traps focus and returns it to the card on Esc", async () => {
    stack();
    const { card, dialog, user } = await openFromCard();
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.contains(document.activeElement)).toBe(true);
    for (let i = 0; i < 12; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement), `after tab ${String(i + 1)}`).toBe(true);
    }
    for (let i = 0; i < 12; i++) {
      await user.tab({ shift: true });
      expect(dialog.contains(document.activeElement), `after shift+tab ${String(i + 1)}`).toBe(true);
    }
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await waitFor(() => {
      expect(document.activeElement).toBe(card);
    });
  });

  it("Esc after opening from the card goes Back and focuses the card", async () => {
    stack();
    const app = renderApp("/?sort=posted");
    const card = await screen.findByRole("article", { name: TITLE });
    card.focus();
    const user = userEvent.setup();
    const before = app.router.history.length;
    await user.keyboard("{Enter}");
    await screen.findByRole("dialog", { name: TITLE });
    expect(app.router.history.length).toBe(before + 1); // the drawer's entry was pushed
    expect(app.router.history.canGoBack()).toBe(true);
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(app.router.state.location.search).toEqual({ sort: "posted" });
    });
    expect(app.router.history.canGoBack()).toBe(false); // Back, not a new entry: Esc and Back agree
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => {
      expect(document.activeElement).toBe(card);
    });
  });

  it("a pasted job URL closes with replace, focusing its card when it is listed", async () => {
    stack();
    const { app, user } = await openPasted("/?job=5&sort=posted");
    const before = app.router.history.length;
    await user.click(screen.getByRole("button", { name: en["action.close"] }));
    await waitFor(() => {
      expect(app.router.state.location.search).toEqual({ sort: "posted" });
    });
    expect(app.router.history.length).toBe(before); // replaced: no entry to go Back to
    expect(app.router.history.canGoBack()).toBe(false);
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByRole("article", { name: TITLE }));
    });
  });

  it("a pasted job URL of a job the list does not show focuses the heading on close", async () => {
    const api = stack();
    api.jobs = jobsPage([jobCard({ job_id: 6, title: "Another job" })]);
    const { user } = await openPasted();
    await screen.findByRole("article", { name: "Another job" });
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: en["jobs.title"] }));
    });
  });

  it("a click on the card's title pushes job=<id> and opens the drawer, and Back closes it", async () => {
    stack();
    const app = renderApp("/?sort=posted");
    const card = await screen.findByRole("article", { name: TITLE });
    const link = within(card).getByRole("link", { name: TITLE });
    const href = new URL(link.getAttribute("href") ?? "", "http://app.test");
    expect([href.pathname, href.searchParams.get("job"), href.searchParams.get("sort")]).toEqual(["/", "5", "posted"]);
    // a modifier click is the browser's (a new tab): nothing prevents it, and the page does not move
    let leftToTheBrowser = false;
    const stop = (event: MouseEvent) => {
      leftToTheBrowser = !event.defaultPrevented;
      event.preventDefault(); // jsdom does not navigate
    };
    document.addEventListener("click", stop);
    fireEvent.click(link, { ctrlKey: true });
    document.removeEventListener("click", stop);
    expect(leftToTheBrowser).toBe(true);
    expect(app.router.state.location.search).toEqual({ sort: "posted" });
    const before = app.router.history.length;
    await userEvent.setup().click(link);
    expect(await screen.findByRole("dialog", { name: TITLE })).toBeTruthy();
    expect(app.router.state.location.search).toEqual({ sort: "posted", job: "5" });
    expect(app.router.history.length).toBe(before + 1); // pushed
    act(() => {
      app.router.history.back();
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(app.router.state.location.search).toEqual({ sort: "posted" });
  });

  it("reopens on the survivor of a merged job", async () => {
    const api = stack();
    api.routes.set("GET /api/jobs/5", () => problem(404, "not_found", { merged_into_job_id: 9 }));
    api.routes.set("GET /api/jobs/9", () =>
      json(jobDetail({ job: { ...jobDetail().job, job_id: 9, title: "Backend Engineer (survivor)" }, user: userActions(9, { seen_at: NOW.toISOString() }) })),
    );
    const app = renderApp("/?job=5");
    expect(await screen.findByRole("dialog", { name: "Backend Engineer (survivor)" })).toBeTruthy();
    expect(app.router.state.location.search).toEqual({ job: "9" });
    expect(app.router.history.length).toBe(1); // replaced
    expect(await screen.findByText(en["drawer.merged"])).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    await settled();
    expect(requestsTo(api, "GET /api/jobs/5")).toHaveLength(1); // redirected once, never read again
    expect(screen.getAllByText(en["drawer.merged"])).toHaveLength(1);
  });

  it("a vanished job shows the gone state, not a problem", async () => {
    const api = stack();
    api.routes.set("GET /api/jobs/5", () => problem(404, "not_found", { merged_into_job_id: null }));
    const app = renderApp("/?job=5");
    const dialog = await screen.findByRole("dialog", { name: en["drawer.title"] });
    expect(await within(dialog).findByText(en["drawer.gone"])).toBeTruthy();
    expect(within(dialog).queryByRole("alert")).toBeNull(); // no problem box
    expect(within(dialog).queryByText(en["problem.not_found"])).toBeNull();
    const user = userEvent.setup();
    await user.click(within(dialog).getByRole("button", { name: en["drawer.close"] }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(app.router.state.location.search).toEqual({});
  });
});

describe("DetailDrawer: seen_on_open", () => {
  it("sends seen_on_open once", async () => {
    const api = stack();
    // under StrictMode, whose effects run twice
    const app = createApp({ history: createMemoryHistory({ initialEntries: ["/?job=5"] }) });
    render(<StrictMode><App app={app} /></StrictMode>);
    const dialog = await screen.findByRole("dialog", { name: TITLE });
    await waitFor(() => {
      expect(bodies(api, "PUT /api/jobs/5/actions")).toEqual([{ seen_on_open: true }]);
    });
    // a write reads the detail again, which still says unseen: no second seen_on_open
    const user = userEvent.setup();
    await user.click(within(dialog).getByRole("button", { name: en["card.save"] }));
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs/5").length).toBeGreaterThan(1);
    });
    await settled();
    expect(bodies(api, "PUT /api/jobs/5/actions")).toEqual([{ seen_on_open: true }, { saved: true }]);
  });

  it("a job already seen sends none", async () => {
    const api = stack(jobDetail({ user: userActions(5, { seen_at: "2026-11-02T08:00:00Z" }) }));
    await openPasted();
    await settled();
    expect(requestsTo(api, "PUT /api/jobs/5/actions")).toHaveLength(0);
  });

  it("seen_on_open patches the list without a refetch", async () => {
    const api = stack();
    renderApp("/");
    const card = await screen.findByRole("article", { name: TITLE });
    expect(within(card).getByText(en["card.new"])).toBeTruthy();
    card.focus();
    await userEvent.setup().keyboard("{Enter}");
    await screen.findByRole("dialog", { name: TITLE });
    await waitFor(() => {
      expect(within(card).queryByText(en["card.new"])).toBeNull();
    });
    await settled();
    expect(requestsTo(api, "GET /api/jobs")).toHaveLength(1); // the list was not read again
    expect(requestsTo(api, "GET /api/jobs/5")).toHaveLength(1);
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: en["drawer.seen"] }).getAttribute("aria-pressed")).toBe("true");
  });
});

describe("DetailDrawer: the header and the score", () => {
  it("shows the job's facts, its actions and its links", async () => {
    stack(jobDetail({ user: userActions(5, { seen_at: "2026-11-02T08:00:00Z", applied_at: "2026-11-02T09:00:00Z" }) }));
    const { dialog } = await openPasted();
    const text = dialog.textContent;
    expect(within(dialog).getByText("Acme GmbH")).toBeTruthy();
    expect(text).toContain(`Berlin · ${en["card.mode.hybrid"]} ${en["drawer.structured"].replace("{{source}}", "Lever")}`);
    expect(text).toContain(en["employment.full_time"]);
    expect(text).toContain(`€70,000–€85,000 ${en["salary_period.year"]} ${en["drawer.salary_stated"]}`);
    expect(text).toContain(`${en["card.posted"]} ≈ 2 days ago ${en["drawer.precision"].replace("{{precision}}", en["posted_precision.day"])}`);
    expect(text).toContain(en["filters.availability.active"]);
    const pressed = (name: string) => within(dialog).getByRole("button", { name }).getAttribute("aria-pressed");
    expect([pressed(en["drawer.seen"]), pressed(en["card.save"]), pressed(en["card.mark_applied"]), pressed(en["card.hide"])]).toEqual([
      "true",
      "false",
      "true",
      "false",
    ]);
    const open = within(dialog).getByRole("link", { name: new RegExp(en["card.open_ad"]) });
    expect(open.getAttribute("href")).toBe("https://jobs.lever.co/acme/abc-123/apply");
    expect(open.getAttribute("rel")).toBe(REL);
    expect(within(dialog).queryByText(en["badge.adzuna"])).toBeNull(); // not an Adzuna ad
  });

  it("an Adzuna estimate, a single amount, and the Adzuna badge with its link", async () => {
    const detail = jobDetail();
    stack(
      jobDetail({
        job: { ...detail.job, source_ids: ["adzuna"], work_mode_source_id: null, salary: { min: "60000", max: null, currency: "EUR", period: "year", kind: "estimate" } },
        links: { open_ad: "https://www.adzuna.de/details/1", adzuna: "https://www.adzuna.de/details/1" },
        user: userActions(5, { seen_at: NOW.toISOString() }),
      }),
    );
    const { dialog } = await openPasted();
    expect(dialog.textContent).toContain(`€60,000 ${en["salary_period.year"]} ${en["drawer.salary_estimate"]}`);
    expect(dialog.textContent).not.toContain("(structured");
    const badge = within(dialog).getByRole("link", { name: new RegExp(en["badge.adzuna"]) });
    expect(badge.getAttribute("href")).toBe("https://www.adzuna.de/details/1");
    expect(badge.getAttribute("rel")).toBe(REL);
  });

  it("Seen clears a job seen at open, and the refetch that follows sends no seen_on_open", async () => {
    const api = stack();
    let seenAt: string | null = "2026-11-02T08:00:00Z";
    api.routes.set("GET /api/jobs/5", () => json(jobDetail({ user: userActions(5, { seen_at: seenAt }) })));
    api.routes.set("PUT /api/jobs/5/actions", (request) => {
      const body = JSON.parse(request.body ?? "{}") as Record<string, unknown>;
      seenAt = body.seen === false ? null : NOW.toISOString();
      return json({ job: userActions(5, { seen_at: seenAt }), members: [] });
    });
    const { dialog, user } = await openPasted();
    const seen = within(dialog).getByRole("button", { name: en["drawer.seen"] });
    expect(seen.getAttribute("aria-pressed")).toBe("true");
    await user.click(seen);
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs/5").length).toBeGreaterThan(1); // the write read the detail again
    });
    await settled();
    expect(bodies(api, "PUT /api/jobs/5/actions")).toEqual([{ seen: false }]);
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: en["drawer.seen"] }).getAttribute("aria-pressed")).toBe("false");
  });

  it("Seen sends seen, never with seen_on_open", async () => {
    const api = stack(jobDetail({ user: userActions(5, { seen_at: "2026-11-02T08:00:00Z" }) }));
    const { dialog, user } = await openPasted();
    await user.click(within(dialog).getByRole("button", { name: en["drawer.seen"] }));
    await waitFor(() => {
      expect(bodies(api, "PUT /api/jobs/5/actions")).toEqual([{ seen: false }]);
    });
  });

  it("shows the score line, the critic's outcome, the versions and the summary", async () => {
    stack(jobDetail({ user: userActions(5, { seen_at: NOW.toISOString() }) }));
    const { dialog } = await openPasted();
    const score = within(dialog).getByRole("region", { name: en["drawer.score"] });
    expect(within(score).getByRole("img", { name: "Score 82, range 74 to 90" })).toBeTruthy();
    const text = score.textContent;
    expect(text).toContain("L 74 · U 90 · coverage 85% · confidence high");
    expect(text).toContain(`${en["result_status.accepted"]} (${en["critic_outcome.accepted"]})`); // "accepted (critic ✓)"
    expect(text).toContain("Profile v4 · rubric v1 · prompt 3f2a1b2c… · gen-model/medium → critic-model/high critic");
    const summary = within(score).getByText("A strong match for backend work.");
    expect(summary.getAttribute("lang")).toBe("en");
    expect(within(score).getByRole("table", { name: en["drawer.criteria"] })).toBeTruthy();
    expect(text).toContain(`${en["card.knockouts"]}: ${en["drawer.none"]}`);
    expect(text).toContain(`${en["drawer.caps"]}: ${en["drawer.none"]}`);
    expect(text).toContain(`${en["drawer.industry"]}: ${en["industry_match.neutral"]}`);
  });

  it("names the knockouts, the caps and a critic that did not run", async () => {
    stack(
      jobDetail({
        score: detailScore({
          critic_outcome: "not_run",
          versions: { ...detailScore().versions, critic: null },
          knockouts: [{ code: "degree", binding: true, severity: "hard", status: "unmet", requirement: "master", profile_value: "bachelor" }],
          caps_applied: [{ code: "degree", kind: "hard" }, { code: "industry", kind: "soft" }],
          industry_match: "outside_include",
        }),
        user: userActions(5, { seen_at: NOW.toISOString() }),
      }),
    );
    const { dialog } = await openPasted();
    const text = within(dialog).getByRole("region", { name: en["drawer.score"] }).textContent;
    expect(text).toContain(`${en["result_status.accepted"]} (${en["critic_outcome.not_run"]})`); // "accepted (not reviewed)"
    expect(text).toContain("gen-model/medium");
    expect(text).not.toContain("critic-model");
    expect(text).toContain(en["card.knockout_requires"].replace("{{requirement}}", en["degree_level.master"]));
    expect(text).toContain(`${en["drawer.caps"]}: ${en["cap.degree"]} (${en["cap_kind.hard"]}), ${en["cap.industry"]} (${en["cap_kind.soft"]})`);
    expect(text).toContain(`${en["drawer.industry"]}: ${en["industry_match.outside_include"]}`);
  });

  it("the summary follows the UI language", async () => {
    const api = stack(jobDetail({ user: userActions(5, { seen_at: NOW.toISOString() }) }));
    api.uiLanguage = "de";
    renderApp("/?job=5");
    const summary = await screen.findByText("Eine starke Passung für Backend-Arbeit.");
    expect(summary.getAttribute("lang")).toBe("de");
    expect(screen.queryByText("A strong match for backend work.")).toBeNull();
    expect(screen.getByRole("dialog", { name: TITLE }).textContent).toContain(
      `${de["result_status.accepted"]} (${de["critic_outcome.accepted"]})`, // "angenommen (Prüfung ✓)"
    );
  });

  it("a job without a score shows its state chip text and no criteria", async () => {
    stack(
      jobDetail({
        score: null,
        target: { id: 50, state: "pending", state_reason: null, refusal_category: null, similarity: 0.6, override: "none" },
        bucket: "unscored",
        history: { items: [], next_cursor: null },
        user: userActions(5, { seen_at: NOW.toISOString() }),
      }),
    );
    const { dialog } = await openPasted();
    const score = within(dialog).getByRole("region", { name: en["drawer.score"] });
    expect(within(score).getByText(en["chip.pending"])).toBeTruthy();
    expect(within(score).queryByRole("table")).toBeNull();
    expect(within(score).queryByRole("img")).toBeNull();
  });

  it("a verified not-a-job-ad says so, by the card's bucket", async () => {
    stack(jobDetail({ score: null, bucket: "not_a_job_ad", user: userActions(5, { seen_at: NOW.toISOString() }) }));
    const { dialog } = await openPasted();
    const score = within(dialog).getByRole("region", { name: en["drawer.score"] });
    expect(within(score).getByText(en["chip.not_a_job_ad"])).toBeTruthy();
  });

  it("a published result without a bucket reads Score pending, never a verified not-a-job-ad", async () => {
    // the T4-c gap: published, but no current instance under the active scope
    stack(jobDetail({ score: null, bucket: null, user: userActions(5, { seen_at: NOW.toISOString() }) }));
    const { dialog } = await openPasted();
    const score = within(dialog).getByRole("region", { name: en["drawer.score"] });
    expect(within(score).getByText(en["drawer.score_pending"])).toBeTruthy();
    expect(within(score).queryByText(en["chip.not_a_job_ad"])).toBeNull();
  });

  it("a contested result's status line says contested once", async () => {
    const score = detailScore({ result_status: "contested", critic_outcome: "contested" });
    stack(jobDetail({ score, user: userActions(5, { seen_at: NOW.toISOString() }) }));
    const { dialog } = await openPasted();
    const region = within(dialog).getByRole("region", { name: en["drawer.score"] });
    const line = within(region).getByText(en["result_status.contested"]);
    expect(line.textContent).toBe(en["result_status.contested"]); // not "contested (contested)"
    expect(region.textContent).not.toContain(`${en["result_status.contested"]} (`);
  });
});

describe("DetailDrawer: the description", () => {
  it("renders description markup as text", async () => {
    const text = '<script>alert(1)</script><img src=x onerror="alert(1)">Python\n<a href="javascript:alert(1)">apply</a>';
    stack(jobDetail({ description: { text, lang: "en", completeness: "full", from_job_id: 5 }, score: null, user: userActions(5, { seen_at: NOW.toISOString() }) }));
    const { dialog } = await openPasted();
    const panel = within(dialog).getByRole("tabpanel", { name: en["drawer.tab.description"] });
    expect(panel.querySelector("script, img, a")).toBeNull();
    expect(within(panel).getByText('<script>alert(1)</script><img src=x onerror="alert(1)">Python')).toBeTruthy();
    expect(within(panel).getByText('<a href="javascript:alert(1)">apply</a>')).toBeTruthy();
  });

  it("fact evidence is highlighted too", async () => {
    const score = detailScore({
      fact_evidence: [
        { subject: "tech:postgresql", source_kind: "description", field_name: null, quote: "PostgreSQL", span_start: 47, span_end: 57 },
        { subject: "tech:python", source_kind: "description", field_name: null, quote: "Python", span_start: null, span_end: null },
      ],
    });
    stack(jobDetail({ score, user: userActions(5, { seen_at: NOW.toISOString() }) }));
    const { dialog } = await openPasted();
    const panel = within(dialog).getByRole("tabpanel", { name: en["drawer.tab.description"] });
    expect(Array.from(panel.querySelectorAll("mark")).map((m) => m.textContent)).toEqual(["Backend services", "FastAPI", "PostgreSQL"]);
    expect(panel.querySelector("div[lang]")?.getAttribute("lang")).toBe("en");
    expect(Array.from(panel.querySelectorAll("p")).map((p) => p.textContent)).toEqual(DESCRIPTION.split("\n"));
  });

  it("a deferred member names the representative's description", async () => {
    const own = jobDetail();
    const detail = jobDetail({
      description: { ...own.description, from_job_id: 9 },
      score: detailScore({ scored_via_job_id: 9 }),
      target: { id: 50, state: "deferred_duplicate", state_reason: null, refusal_category: null, similarity: 0.7, override: "none" },
      group: {
        id: 3,
        status: "active",
        representative_job_id: 9,
        unscorable_reason: null,
        election_reason: null,
        members: [
          { job_id: 9, title: "Backend Engineer (Lever)", company: "Acme GmbH", source_ids: ["lever"], locations: ["Berlin"], completeness: "full", availability: "active", target_state: "published_accepted", score_individually: false, is_representative: true },
          { job_id: 5, title: TITLE, company: "Acme GmbH", source_ids: ["lever"], locations: ["Berlin"], completeness: "full", availability: "active", target_state: "deferred_duplicate", score_individually: false, is_representative: false },
        ],
      },
      user: userActions(5, { seen_at: NOW.toISOString() }),
    });
    const api = stack(detail);
    api.routes.set("GET /api/jobs/9", () =>
      json(jobDetail({ job: { ...own.job, job_id: 9, title: "Backend Engineer (Lever)" }, user: userActions(9, { seen_at: NOW.toISOString() }) })),
    );
    const { app, dialog, user } = await openPasted();
    const panel = within(dialog).getByRole("tabpanel", { name: en["drawer.tab.description"] });
    expect(panel.textContent).toContain(`${en["drawer.description_from"]} Backend Engineer (Lever)`);
    const score = within(dialog).getByRole("region", { name: en["drawer.score"] });
    expect(score.textContent).toContain(`${en["drawer.scored_via"]} Backend Engineer (Lever)`);
    // the tabs start again at Description on the next job
    await user.click(within(dialog).getByRole("tab", { name: en["drawer.tab.history"] }));
    await user.click(within(score).getByRole("button", { name: "Backend Engineer (Lever)" }));
    expect(await screen.findByRole("dialog", { name: "Backend Engineer (Lever)" })).toBeTruthy();
    expect(app.router.state.location.search).toEqual({ job: "9" });
    expect(app.router.history.length).toBe(1); // replaced
    expect(screen.getByRole("tab", { name: en["drawer.tab.description"] }).getAttribute("aria-selected")).toBe("true");
  });

  it("names a representative missing from the members by its id", async () => {
    const own = jobDetail();
    stack(jobDetail({ description: { ...own.description, from_job_id: 9 }, user: userActions(5, { seen_at: NOW.toISOString() }) }));
    const { dialog } = await openPasted();
    const panel = within(dialog).getByRole("tabpanel", { name: en["drawer.tab.description"] });
    expect(within(panel).getByRole("button", { name: en["drawer.job_ref"].replace("{{id}}", "9") })).toBeTruthy();
  });
});

describe("DetailDrawer: the tabs", () => {
  it("has the same five tabs for every job, and an empty one says so", async () => {
    stack(jobDetail({ history: { items: [], next_cursor: null }, user: userActions(5, { seen_at: NOW.toISOString() }) }));
    const { dialog, user } = await openPasted();
    const tabs = within(within(dialog).getByRole("tablist", { name: en["drawer.tabs"] })).getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual([
      en["drawer.tab.description"],
      en["drawer.tab.contested"],
      en["drawer.tab.provenance"],
      en["drawer.tab.history"],
      en["drawer.tab.feedback"],
    ]);
    await user.click(tabs[1]);
    expect(within(within(dialog).getByRole("tabpanel")).getByText(en["contested.none"])).toBeTruthy();
    await user.click(tabs[3]);
    expect(within(within(dialog).getByRole("tabpanel")).getByText(en["history.none"])).toBeTruthy();
    await user.click(tabs[4]);
    expect(within(within(dialog).getByRole("tabpanel")).getByRole("form", { name: en["feedback.title"] })).toBeTruthy();
  });
});
