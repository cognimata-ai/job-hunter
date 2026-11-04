import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import codes from "../i18n/codes.json";
import de from "../i18n/de.json";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import {
  type FakeApi,
  fakeApi,
  jobCard,
  jobsPage,
  json,
  problem,
  requestsTo,
  spendOut,
  statusOut,
} from "../test/api";
import { NOW, renderApp, renderInProviders, settled } from "../test/app";
import { JobCard } from "./JobCard";

type JobCardOut = components["schemas"]["JobCardOut"];
type TargetOut = components["schemas"]["TargetOut"];
type ScoreOut = components["schemas"]["ScoreOut"];
type KnockoutOut = components["schemas"]["KnockoutOut"];

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const REL = "noopener noreferrer nofollow";

// The server's order of the badges (`jobs.cards.Badge`).
const BADGE_ORDER: JobCardOut["badges"] = [
  "contested",
  "partial",
  "shortened",
  "possibly_not_a_job_ad",
  "injection_suspected",
  "stale",
  "closed",
  "adzuna",
];

const SCORE: ScoreOut = {
  mid: "82.40",
  low: "74.00",
  high: "89.50",
  coverage: "0.850",
  confidence: "high",
  result_status: "accepted",
};

afterEach(async () => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW }); // the client's clock; timers stay real
});

function target(state: string, extra: Partial<TargetOut> = {}): TargetOut {
  return { id: 50, state, state_reason: null, refusal_category: null, similarity: 0.62, override: "none", ...extra };
}

function knockout(extra: Partial<KnockoutOut>): KnockoutOut {
  return {
    code: "mandatory_language",
    binding: true,
    severity: "hard",
    status: "unmet",
    requirement: null,
    profile_value: null,
    ...extra,
  };
}

function scored(extra: Partial<JobCardOut> = {}): JobCardOut {
  return jobCard({ score: SCORE, target: target("published_accepted", { similarity: 0.7 }), bucket: "scored", ...extra });
}

/** The answer of `PUT /api/jobs/{id}/actions`: the job's row after the write. */
function actionsOut(jobId: number, row: { saved_at?: string | null; hidden_at?: string | null } = {}) {
  return {
    job: {
      job_id: jobId,
      seen_at: null,
      saved_at: row.saved_at ?? null,
      applied_at: null,
      hidden_at: row.hidden_at ?? null,
      note: null,
      updated_at: NOW.toISOString(),
    },
    members: [],
  };
}

/** Renders `/` with `items` as the first page, and returns their cards once listed. */
async function cardsOf(api: FakeApi, items: JobCardOut[]): Promise<HTMLElement[]> {
  api.jobs = jobsPage(items);
  renderApp("/");
  const list = await screen.findByRole("list", { name: en["jobs.title"] });
  return within(list).getAllByRole("article");
}

function bodies(api: FakeApi, call: string): unknown[] {
  return requestsTo(api, call).map((r) => JSON.parse(r.body ?? "null") as unknown);
}

function texts(list: HTMLElement): (string | null)[] {
  return within(list).getAllByRole("listitem").map((li) => li.textContent);
}

describe("JobCard", () => {
  it("renders a title with markup as text", async () => {
    const api = fakeApi({ signedIn: true });
    const title = "Dev <img src=x onerror=alert(1)>";
    const [card] = await cardsOf(api, [
      jobCard({ title, company: "<b>Acme</b> & Co", locations: ["<i>Berlin</i>"] }),
    ]);
    expect(within(card).getByRole("heading", { name: title })).toBeTruthy();
    expect(screen.getByRole("article", { name: title })).toBe(card); // the card is named by its heading
    expect(document.querySelector("img")).toBeNull();
    expect(card.querySelector("b, i")).toBeNull();
    expect(card.textContent).toContain("<b>Acme</b> & Co");
    expect(card.textContent).toContain("<i>Berlin</i>");
  });

  it("outbound links carry rel", async () => {
    const api = fakeApi({ signedIn: true });
    const adzuna = "https://www.adzuna.de/details/5929715902?utm_source=x";
    const [linked, bare] = await cardsOf(api, [
      jobCard({ source_ids: ["adzuna"], badges: ["partial", "adzuna"], links: { open_ad: adzuna, adzuna } }),
      jobCard({ job_id: 6, title: "No link", links: { open_ad: null, adzuna: null } }),
    ]);
    const links = within(linked).getAllByRole("link");
    expect(links).toHaveLength(2); // Open ad and the Adzuna attribution
    for (const link of links) {
      expect(link.getAttribute("rel")).toBe(REL);
      expect(link.getAttribute("target")).toBe("_blank");
      expect(link.getAttribute("href")).toBe(adzuna);
    }
    expect(within(linked).getByRole("link", { name: `${en["card.open_ad"]} ${en["link.new_tab"]}` })).toBeTruthy();
    expect(within(bare).queryByRole("link")).toBeNull(); // no Open ad without a link
  });

  it("NEW for an unseen card and markers for saved and applied", async () => {
    const api = fakeApi({ signedIn: true });
    const user = { seen_at: null, saved_at: null, applied_at: null, hidden_at: null, has_note: false };
    const [fresh, savedUnseen, acted] = await cardsOf(api, [
      jobCard({ job_id: 1, title: "Unseen" }),
      jobCard({ job_id: 2, title: "Saved, not seen", user: { ...user, saved_at: "2026-11-02T08:00:00Z" } }),
      jobCard({
        job_id: 3,
        title: "Seen and applied",
        user: { ...user, seen_at: "2026-11-02T08:00:00Z", saved_at: "2026-11-02T08:01:00Z", applied_at: "2026-11-02T09:00:00Z" },
      }),
    ]);
    expect(within(fresh).getByText(en["card.new"])).toBeTruthy();
    expect(within(fresh).queryByText(en["card.saved"])).toBeNull();
    expect(within(savedUnseen).getByText(en["card.new"])).toBeTruthy();
    expect(within(savedUnseen).getByText(en["card.saved"])).toBeTruthy();
    expect(within(savedUnseen).queryByText(en["card.applied"])).toBeNull();
    expect(within(acted).queryByText(en["card.new"])).toBeNull();
    expect(within(acted).getByText(en["card.saved"])).toBeTruthy();
    expect(within(acted).getByText(en["card.applied"])).toBeTruthy();
    const pressed = (card: HTMLElement, name: string) =>
      within(card).getByRole("button", { name }).getAttribute("aria-pressed");
    expect(pressed(acted, en["card.save"])).toBe("true");
    expect(pressed(acted, en["card.mark_applied"])).toBe("true");
    expect(pressed(acted, en["card.hide"])).toBe("false");
    expect(pressed(fresh, en["card.save"])).toBe("false");
  });

  it("shows the card's facts: company, places, work mode, dates and sources", async () => {
    const api = fakeApi({ signedIn: true });
    const [known, unknown] = await cardsOf(api, [
      jobCard({
        locations: ["Berlin", "München"],
        work_modes: ["remote", "hybrid"],
        posted_at: "2026-10-31T12:00:00Z",
        posted_precision: "ingest",
        first_seen_at: "2026-11-01T07:00:00Z",
        last_seen_at: "2026-11-02T07:00:00Z", // yesterday in Berlin: shown
        source_ids: ["adzuna", "smartrecruiters", "newboard"],
      }),
      jobCard({
        job_id: 6,
        title: "Unknown mode",
        work_modes: ["remote", "hybrid", "onsite"],
        work_mode_known: false,
        posted_at: null,
        posted_precision: "unknown",
        last_seen_at: "2026-11-02T23:30:00Z", // 00:30 today in Berlin: not shown
      }),
    ]);
    expect(within(known).getByText("Acme GmbH")).toBeTruthy();
    expect(known.textContent).toContain("Berlin · München");
    expect(known.textContent).toContain("remote / hybrid");
    expect(known.textContent).toContain("Posted ≈ 3 days ago (ingest date)");
    expect(known.textContent).toContain("first seen 2 days ago");
    expect(known.textContent).toContain("last seen yesterday");
    expect(texts(within(known).getByRole("list", { name: en["card.sources"] }))).toEqual([
      "Adzuna",
      "SmartRecruiters",
      "newboard", // an id the catalogue lacks, verbatim
    ]);
    expect(unknown.textContent).toContain(en["card.mode_unknown"]);
    expect(unknown.textContent).not.toContain("onsite");
    expect(unknown.textContent).toContain(`Posted ${en["posted.unknown"]}`);
    expect(unknown.textContent).not.toContain("last seen");
  });

  it("knockout badge text", async () => {
    const api = fakeApi({ signedIn: true });
    const [card] = await cardsOf(api, [
      scored({
        knockouts: [
          knockout({ code: "degree", requirement: "master (Computer Science)", profile_value: "bachelor" }),
          knockout({ code: "work_permit", binding: false, status: "uncomparable" }),
          knockout({ code: "onsite_required", status: "met" }), // met: not shown
        ],
      }),
    ]);
    expect(texts(within(card).getByRole("list", { name: en["card.knockouts"] }))).toEqual([
      "⚑ Requires master's degree (Computer Science) — you: bachelor's degree", // the levels by their codes
      `⚑ ${en["knockout.work_permit"]} (unconfirmed)`,
    ]);
  });

  it("a knockout without a profile value omits the you part", async () => {
    const api = fakeApi({ signedIn: true });
    const [card] = await cardsOf(api, [
      scored({
        knockouts: [
          knockout({ requirement: "de C1" }),
          knockout({ code: "work_permit", profile_value: "requires_sponsorship" }), // no requirement: its name
        ],
      }),
    ]);
    expect(texts(within(card).getByRole("list", { name: en["card.knockouts"] }))).toEqual([
      "⚑ Requires German C1",
      `⚑ ${en["knockout.work_permit"]} — you: requires sponsorship`,
    ]);
  });

  it("knockout values are named by their codes, in German too, and an unknown one stays as it is", async () => {
    const api = fakeApi({ signedIn: true });
    api.uiLanguage = "de";
    api.jobs = jobsPage([
      scored({
        knockouts: [
          knockout({ code: "degree", binding: false, requirement: "master (Informatik)", profile_value: "bachelor" }),
          knockout({ code: "work_permit", profile_value: "requires_sponsorship" }),
          knockout({ code: "degree", requirement: "diploma (Physik)", profile_value: "magister" }),
          knockout({ code: "degree", requirement: "(Informatik)", profile_value: "none" }), // a field, no level
        ],
      }),
    ]);
    renderApp("/");
    const list = await screen.findByRole("list", { name: de["jobs.title"] });
    const [card] = within(list).getAllByRole("article");
    expect(texts(within(card).getByRole("list", { name: de["card.knockouts"] }))).toEqual([
      "⚑ Erfordert Master (Informatik) – Profil: Bachelor (unbestätigt)",
      `⚑ ${de["knockout.work_permit"]} – Profil: benötigt Visa-Sponsoring`,
      "⚑ Erfordert diploma (Physik) – Profil: magister",
      `⚑ Erfordert (Informatik) – Profil: ${de["degree_level.none"]}`,
    ]);
  });

  it("a contract_type requirement names its employment types in the locale's list form", async () => {
    const api = fakeApi({ signedIn: true });
    const knockouts = [
      knockout({ code: "contract_type", requirement: "internship, working_student" }),
      knockout({ code: "contract_type", binding: false, requirement: "freelance, gig, part_time" }), // `gig`: no code
    ];
    const [card] = await cardsOf(api, [scored({ knockouts })]);
    expect(texts(within(card).getByRole("list", { name: en["card.knockouts"] }))).toEqual([
      `⚑ Requires ${en["employment.internship"]} and ${en["employment.working_student"]}`,
      `⚑ Requires ${en["employment.freelance"]}, gig and ${en["employment.part_time"]} (unconfirmed)`,
    ]);
    cleanup();
    api.uiLanguage = "de";
    api.jobs = jobsPage([scored({ knockouts })]);
    renderApp("/");
    const list = await screen.findByRole("list", { name: de["jobs.title"] });
    const [german] = within(list).getAllByRole("article");
    expect(texts(within(german).getByRole("list", { name: de["card.knockouts"] }))).toEqual([
      `⚑ Erfordert ${de["employment.internship"]} und ${de["employment.working_student"]}`,
      `⚑ Erfordert ${de["employment.freelance"]}, gig und ${de["employment.part_time"]} (unbestätigt)`,
    ]);
  });

  it("the language of a mandatory_language requirement is named", async () => {
    const api = fakeApi({ signedIn: true });
    api.uiLanguage = "de";
    const items = [
      scored({
        knockouts: [
          knockout({ binding: false, requirement: "de C1", profile_value: "B2" }),
          knockout({ requirement: "xx B1" }), // an unknown code stays as it is
          knockout({ requirement: "Klingonisch C1" }), // the ad's own name of an `other` language
        ],
      }),
    ];
    api.jobs = jobsPage(items);
    renderApp("/");
    const list = await screen.findByRole("list", { name: de["jobs.title"] });
    const [card] = within(list).getAllByRole("article");
    expect(texts(within(card).getByRole("list", { name: de["card.knockouts"] }))).toEqual([
      "⚑ Erfordert Deutsch C1 – Profil: B2 (unbestätigt)",
      "⚑ Erfordert xx B1",
      "⚑ Erfordert Klingonisch C1",
    ]);
  });

  it("every badge has text", async () => {
    expect([...BADGE_ORDER].sort()).toEqual(codes.badge); // every badge of the catalogue (the backend's Badge)
    const api = fakeApi({ signedIn: true });
    const [all, duplicate, unscorable] = await cardsOf(api, [
      jobCard({
        badges: BADGE_ORDER,
        links: { open_ad: null, adzuna: "https://www.adzuna.de/details/1" },
        previous: { mid: "72.50", profile_version_no: 3 },
      }),
      scored({
        job_id: 6,
        title: "A member with its own score",
        possible_duplicate_of: { job_id: 9, title: "Backend Engineer (m/w/d)", source_ids: ["jsearch"] },
      }),
      jobCard({
        job_id: 7,
        title: "A member of an unscorable group",
        target: target("filtered_out"),
        bucket: "filtered",
        filter_reasons: ["outside_germany"],
        group_badge: { group_id: 4, unscorable_reason: "all_filtered" },
      }),
    ]);
    const badges = (card: HTMLElement) => within(card).getByRole("list", { name: en["card.badges"] });
    expect(texts(badges(all))).toEqual([
      ...BADGE_ORDER.map((code) => en[`badge.${code}` as keyof typeof en] + (code === "adzuna" ? ` ${en["link.new_tab"]}` : "")),
      "previous: 73 (profile v3)",
    ]);
    expect(texts(badges(duplicate))).toEqual(["possible duplicate of Backend Engineer (m/w/d)"]);
    expect(texts(badges(unscorable))).toEqual([`Not scorable: ${en["group_unscorable.all_filtered"]}`]);
    for (const card of [all, duplicate, unscorable]) {
      for (const item of within(badges(card)).getAllByRole("listitem")) {
        expect(item.textContent.trim()).not.toBe("");
      }
    }
  });

  it("the Adzuna badge links with rel", async () => {
    const api = fakeApi({ signedIn: true });
    const href = "https://www.adzuna.de/details/1?se=abc";
    const adzuna = { source_ids: ["adzuna"], badges: ["partial", "adzuna"] as JobCardOut["badges"] };
    const [linked, plain] = await cardsOf(api, [
      jobCard({ job_id: 1, title: "Linked", ...adzuna, links: { open_ad: null, adzuna: href } }),
      jobCard({ job_id: 2, title: "Plain", ...adzuna, links: { open_ad: null, adzuna: null } }),
    ]);
    const link = within(linked).getByRole("link", { name: `${en["badge.adzuna"]} ${en["link.new_tab"]}` });
    expect(link.getAttribute("href")).toBe(href);
    expect(link.getAttribute("rel")).toBe(REL);
    expect(link.getAttribute("target")).toBe("_blank");
    expect(within(plain).queryByRole("link")).toBeNull();
    expect(within(within(plain).getByRole("list", { name: en["card.badges"] })).getByText(en["badge.adzuna"])).toBeTruthy();
  });
});


describe("the score or the chip", () => {
  it("a scored card shows its score badge and no chip", async () => {
    const api = fakeApi({ signedIn: true });
    const [card] = await cardsOf(api, [scored({ badges: ["contested"], score: { ...SCORE, result_status: "contested" } })]);
    expect(within(card).getByRole("img", { name: "Score 82, range 74 to 90" })).toBeTruthy();
    expect(within(card).queryByRole("meter")).toBeNull();
    expect(within(card).queryByRole("button", { name: /^Score now/ })).toBeNull();
    expect(within(card).queryByText(en["target_state.published_accepted"])).toBeNull();
  });

  it("a scored card that fails its hard filter shows its score and the filtered badge, no chip", async () => {
    const api = fakeApi({ signedIn: true });
    const [byReason, byKeyword, passing] = await cardsOf(api, [
      scored({ bucket: "filtered", filter_reasons: ["excluded_company"], badges: ["contested"] }),
      scored({
        job_id: 6,
        title: "Praktikum Backend",
        bucket: "filtered",
        filter_reasons: ["excluded_keyword"],
        filter_keyword: "Praktikum",
      }),
      scored({ job_id: 7, title: "Passing" }),
    ]);
    const badges = (card: HTMLElement) => texts(within(card).getByRole("list", { name: en["card.badges"] }));
    expect(within(byReason).getByRole("img", { name: "Score 82, range 74 to 90" })).toBeTruthy();
    expect(badges(byReason)).toEqual([en["badge.contested"], `Filtered: ${en["filter_reason.excluded_company"]}`]);
    expect(badges(byKeyword)).toEqual(["Filtered: excluded keyword 'Praktikum'"]); // the chip's own text
    for (const card of [byReason, byKeyword]) {
      expect(within(card).queryByRole("button", { name: en["card.score_anyway"] })).toBeNull();
      expect(within(card).queryByRole("meter")).toBeNull();
      expect(within(card).queryByText(en["chip.pending"])).toBeNull();
    }
    expect(within(passing).queryByRole("list", { name: en["card.badges"] })).toBeNull();
  });

  it("Score anyway is disabled with the cap's text under cap_reached", async () => {
    const api = fakeApi({ signedIn: true });
    api.status = statusOut({ banners: [{ code: "cap_reached", severity: "critical", params: {} }] });
    const [card] = await cardsOf(api, [
      jobCard({ target: target("filtered_out"), bucket: "filtered", filter_reasons: ["excluded_company"] }),
    ]);
    const button = within(card).getByRole("button", { name: en["card.score_anyway"] });
    await waitFor(() => {
      expect((button as HTMLButtonElement).disabled).toBe(true);
    });
    const hint = document.getElementById(button.getAttribute("aria-describedby") ?? "");
    expect(hint?.textContent).toBe(en["card.score_now_cap"]);
    expect(card.contains(hint)).toBe(true);
    await userEvent.setup().click(button);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(requestsTo(api, "POST /api/jobs/5/score-now")).toHaveLength(0);
  });

  it("pending chip with the similarity label", async () => {
    const api = fakeApi({ signedIn: true });
    const [pending, without] = await cardsOf(api, [
      jobCard(),
      jobCard({ job_id: 6, title: "No similarity", target: target("pending", { similarity: null }) }),
    ]);
    expect(within(pending).getByText(en["chip.pending"])).toBeTruthy();
    const meter = within(pending).getByRole("meter", { name: en["card.similarity"] });
    expect(meter.getAttribute("min")).toBe("0");
    expect(meter.getAttribute("max")).toBe("1");
    expect(meter.getAttribute("value")).toBe("0.62");
    expect(meter.getAttribute("aria-valuetext")).toBe("0.62"); // read as shown, not as a percentage
    expect(within(pending).getByText("0.62")).toBeTruthy();
    expect(within(pending).queryByRole("img")).toBeNull(); // no score badge
    expect(within(without).getByText(en["chip.pending"])).toBeTruthy();
    expect(within(without).queryByRole("meter")).toBeNull();
  });

  it("a chip for each state", async () => {
    const api = fakeApi({ signedIn: true });
    const cases: [Partial<JobCardOut>, string][] = [
      [{ target: target("pending") }, en["chip.pending"]],
      [{ target: target("budget_deferred") }, en["chip.budget_deferred"]],
      [{ target: target("in_progress") }, en["chip.in_progress"]],
      [{ target: target("in_batch") }, en["chip.in_batch"]],
      [{ target: target("deferred_duplicate") }, en["chip.deferred_duplicate"]],
      [{ target: target("expired_unscored"), bucket: "expired" }, en["chip.expired_unscored"]],
      [{ target: null }, en["chip.not_queued"]],
      [{ target: target("cancelled") }, en["target_state.cancelled"]],
      [{ target: target("published_accepted"), bucket: "not_a_job_ad" }, en["chip.not_a_job_ad"]],
    ];
    const cards = await cardsOf(
      api,
      cases.map(([extra], i) => jobCard({ job_id: i + 1, title: `Case ${String(i + 1)}`, ...extra })),
    );
    cases.forEach(([, chip], i) => {
      expect(within(cards[i]).getByText(chip), chip).toBeTruthy();
    });
  });

  it("filtered chip names the keyword and Score anyway confirms then sends force", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/jobs/5/score-now", () => json({ target_id: 50, state: "pending" }, 202));
    const [card] = await cardsOf(api, [
      jobCard({
        target: target("filtered_out"),
        bucket: "filtered",
        filter_reasons: ["excluded_keyword"],
        filter_keyword: "Praktikum",
      }),
    ]);
    const chip = "Filtered: excluded keyword 'Praktikum'";
    expect(within(card).getByText(chip)).toBeTruthy();
    expect(within(card).queryByRole("button", { name: /^Score now/ })).toBeNull();
    const user = userEvent.setup();
    await user.click(within(card).getByRole("button", { name: en["card.score_anyway"] }));
    const dialog = await screen.findByRole("alertdialog", { name: en["card.score_anyway_title"] });
    expect(dialog.textContent).toContain(chip); // the dialog names the reason
    expect(requestsTo(api, "POST /api/jobs/5/score-now")).toHaveLength(0);
    const reads = requestsTo(api, "GET /api/jobs").length;
    await user.click(within(dialog).getByRole("button", { name: en["card.score_anyway"] }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    const [post] = requestsTo(api, "POST /api/jobs/5/score-now");
    expect(JSON.parse(post.body ?? "null")).toEqual({ force: true });
    expect(post.headers.get("Idempotency-Key")).toMatch(UUID);
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs").length).toBeGreaterThan(reads);
    });
    expect(await screen.findByText(en["card.score_now_queued"])).toBeTruthy();
  });

  it("a filtered chip without a keyword names its reasons, and Cancel sends nothing", async () => {
    const api = fakeApi({ signedIn: true });
    const [card] = await cardsOf(api, [
      jobCard({ target: target("filtered_out"), bucket: "filtered", filter_reasons: ["outside_germany", "beyond_radius"] }),
    ]);
    const chip = `Filtered: ${en["filter_reason.outside_germany"]}, ${en["filter_reason.beyond_radius"]}`;
    expect(within(card).getByText(chip)).toBeTruthy();
    const user = userEvent.setup();
    await user.click(within(card).getByRole("button", { name: en["card.score_anyway"] }));
    const dialog = await screen.findByRole("alertdialog", { name: en["card.score_anyway_title"] });
    await user.click(within(dialog).getByRole("button", { name: en["action.cancel"] }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(requestsTo(api, "POST /api/jobs/5/score-now")).toHaveLength(0);
  });

  it("failed chip Retry sends a keyed retry", async () => {
    const api = fakeApi({ signedIn: true });
    const answers = [() => problem(503, "internal"), () => json({ target_id: 51, state: "pending" }, 202)];
    api.routes.set("POST /api/targets/51/retry", () => answers.shift()?.() ?? problem(500, "internal"));
    const [card] = await cardsOf(api, [
      jobCard({ target: target("failed_transient", { id: 51, state_reason: "provider_overloaded" }), bucket: "failed" }),
    ]);
    expect(within(card).getByText(en["target_state.failed_transient"])).toBeTruthy(); // "Failed (temporary error)"
    expect(card.textContent).not.toContain("Failed: ");
    expect(within(card).getByText("provider_overloaded").tagName).toBe("CODE");
    expect(within(card).queryByRole("button", { name: /^Score now/ })).toBeNull();
    const user = userEvent.setup();
    await user.click(within(card).getByRole("button", { name: en["action.retry"] }));
    expect(await screen.findByText(en["problem.internal"])).toBeTruthy(); // the 503, as its own toast only
    expect(screen.queryByText(en["toast.transient"])).toBeNull();
    const reads = { jobs: requestsTo(api, "GET /api/jobs").length, status: requestsTo(api, "GET /api/system/status").length };
    await user.click(within(card).getByRole("button", { name: en["action.retry"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/targets/51/retry")).toHaveLength(2);
    });
    const [first, second] = requestsTo(api, "POST /api/targets/51/retry");
    expect(first.headers.get("Idempotency-Key")).toMatch(UUID);
    expect(second.headers.get("Idempotency-Key")).toBe(first.headers.get("Idempotency-Key")); // the same action
    expect(first.body).toBeNull();
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs").length).toBeGreaterThan(reads.jobs);
      expect(requestsTo(api, "GET /api/system/status").length).toBeGreaterThan(reads.status);
    });
  });

  it("a refused chip names the category and offers Retry", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/targets/52/retry", () => json({ target_id: 52, state: "pending" }, 202));
    const [card] = await cardsOf(api, [
      jobCard({ target: target("refused", { id: 52, refusal_category: "policy_violation" }), bucket: "failed" }),
    ]);
    expect(within(card).getByText("Declined by the model (policy_violation)")).toBeTruthy();
    await userEvent.setup().click(within(card).getByRole("button", { name: en["action.retry"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/targets/52/retry")).toHaveLength(1);
    });
    expect(requestsTo(api, "POST /api/targets/52/retry")[0].headers.get("Idempotency-Key")).toMatch(UUID);
  });
});

describe("Score now", () => {
  it("names its estimated cost on the states it serves, read once for the page", async () => {
    const api = fakeApi({ signedIn: true });
    const cards = await cardsOf(api, [
      jobCard({ job_id: 1, title: "Pending", target: target("pending") }),
      jobCard({ job_id: 2, title: "Deferred", target: target("budget_deferred") }),
      jobCard({ job_id: 3, title: "Expired", target: target("expired_unscored"), bucket: "expired" }),
      jobCard({ job_id: 4, title: "In batch", target: target("in_batch") }),
      jobCard({ job_id: 5, title: "Not queued", target: null }),
    ]);
    await waitFor(() => {
      expect(within(cards[0]).getByRole("button", { name: "Score now ~$0.05" })).toBeTruthy();
    });
    expect(within(cards[1]).getByRole("button", { name: "Score now ~$0.05" })).toBeTruthy();
    expect(within(cards[2]).getByRole("button", { name: "Score now ~$0.05" })).toBeTruthy();
    expect(within(cards[3]).queryByRole("button", { name: /^Score now/ })).toBeNull();
    expect(within(cards[4]).queryByRole("button", { name: /^Score now/ })).toBeNull();
    expect(requestsTo(api, "GET /api/system/spend")).toHaveLength(1);
  });

  it("below a cent reads <$0.01", async () => {
    const api = fakeApi({ signedIn: true });
    api.spend = spendOut({ c_hat: "0.004200" });
    const [card] = await cardsOf(api, [jobCard()]);
    expect(await within(card).findByRole("button", { name: "Score now <$0.01" })).toBeTruthy();
  });

  it("without spend data reads Score now", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/system/spend", () => problem(404, "not_found"));
    const [card] = await cardsOf(api, [jobCard()]);
    await settled();
    expect(within(card).getByRole("button", { name: en["card.score_now"] })).toBeTruthy();
  });

  it("Score now disabled with a tooltip under cap_reached", async () => {
    const api = fakeApi({ signedIn: true });
    api.status = statusOut({ banners: [{ code: "cap_reached", severity: "critical", params: {} }] });
    const [card] = await cardsOf(api, [jobCard()]);
    const button = within(card).getByRole("button", { name: /^Score now/ });
    await waitFor(() => {
      expect((button as HTMLButtonElement).disabled).toBe(true);
    });
    const hint = document.getElementById(button.getAttribute("aria-describedby") ?? "");
    expect(hint?.textContent).toBe(en["card.score_now_cap"]);
    expect(card.contains(hint)).toBe(true); // readable next to the button, not only on hover
  });

  it("follows a later status read: disabled when the cap arrives, enabled again when it leaves", async () => {
    const api = fakeApi({ signedIn: true });
    api.jobs = jobsPage([jobCard()]);
    const app = renderApp("/");
    const card = await screen.findByRole("article", { name: "Senior Backend Engineer" });
    const button = await within(card).findByRole<HTMLButtonElement>("button", { name: "Score now ~$0.05" });
    await settled();
    expect(button.disabled).toBe(false);
    expect(button.getAttribute("aria-describedby")).toBeNull();
    const reread = async () => {
      await act(async () => {
        await app.queryClient.invalidateQueries({ queryKey: ["system", "status"] });
      });
    };
    api.status = statusOut({ banners: [{ code: "cap_reached", severity: "critical", params: {} }] });
    await reread();
    await waitFor(() => {
      expect(button.disabled).toBe(true);
    });
    expect(document.getElementById(button.getAttribute("aria-describedby") ?? "")?.textContent).toBe(
      en["card.score_now_cap"],
    );
    api.status = statusOut();
    await reread();
    await waitFor(() => {
      expect(button.disabled).toBe(false);
    });
    expect(button.getAttribute("aria-describedby")).toBeNull();
    expect(within(card).queryByText(en["card.score_now_cap"])).toBeNull();
  });

  it("sends a keyed POST without force, and its answers toast", async () => {
    const api = fakeApi({ signedIn: true });
    const answers = [
      () => problem(409, "budget_exhausted"),
      () => problem(503, "internal"),
      () => json({ target_id: 50, state: "pending" }, 202),
    ];
    api.routes.set("POST /api/jobs/5/score-now", () => answers.shift()?.() ?? problem(500, "internal"));
    const [card] = await cardsOf(api, [jobCard()]);
    const user = userEvent.setup();
    const button = await within(card).findByRole("button", { name: "Score now ~$0.05" });
    await user.click(button);
    expect(await screen.findByText(en["problem.budget_exhausted"])).toBeTruthy();
    await user.click(button);
    expect(await screen.findByText(en["problem.internal"])).toBeTruthy();
    expect(screen.queryByText(en["toast.transient"])).toBeNull();
    const reads = { jobs: requestsTo(api, "GET /api/jobs").length, status: requestsTo(api, "GET /api/system/status").length };
    await user.click(button);
    expect(await screen.findByText(en["card.score_now_queued"])).toBeTruthy();
    // the list and the header's status are read again, the status as the header reads it (a poll)
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs").length).toBeGreaterThan(reads.jobs);
      expect(requestsTo(api, "GET /api/system/status").length).toBeGreaterThan(reads.status);
    });
    expect(requestsTo(api, "GET /api/system/status").at(-1)?.headers.get("X-JH-Background")).toBe("1");
    await settled();
    expect(screen.queryByText(en["toast.transient"])).toBeNull();
    const posts = requestsTo(api, "POST /api/jobs/5/score-now");
    expect(posts.map((p) => JSON.parse(p.body ?? "null") as unknown)).toEqual([{}, {}, {}]);
    const keys = posts.map((p) => p.headers.get("Idempotency-Key"));
    expect(keys[0]).toMatch(UUID);
    expect(keys[1]).not.toBe(keys[0]); // a 409 is an answer: the next click is a new action
    expect(keys[2]).toBe(keys[1]); // a 503 is not: the next click retries it with its key
  });

  it("a conflict or a gone job toasts and refetches the list", async () => {
    const api = fakeApi({ signedIn: true });
    const answers = [() => problem(409, "conflict_state"), () => problem(404, "not_found")];
    api.routes.set("POST /api/jobs/5/score-now", () => answers.shift()?.() ?? problem(500, "internal"));
    const [card] = await cardsOf(api, [jobCard()]);
    const user = userEvent.setup();
    const button = await within(card).findByRole("button", { name: "Score now ~$0.05" });
    let reads = requestsTo(api, "GET /api/jobs").length;
    await user.click(button);
    expect(await screen.findByText(en["problem.conflict_state"])).toBeTruthy();
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs").length).toBe(reads + 1);
    });
    reads += 1;
    await user.click(within(screen.getByRole("article", { name: "Senior Backend Engineer" })).getByRole("button", { name: "Score now ~$0.05" }));
    expect(await screen.findByText(en["problem.not_found"])).toBeTruthy();
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs").length).toBe(reads + 1);
    });
  });
});

describe("QuickActions", () => {
  it("Save sends saved true and a group card sends apply_to_group", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("PUT /api/jobs/5/actions", () => json(actionsOut(5, { saved_at: NOW.toISOString() })));
    api.routes.set("PUT /api/jobs/7/actions", () => json(actionsOut(7)));
    const group = {
      group_id: 3,
      size: 2,
      representative: { job_id: 7, title: "Platform Engineer", source_ids: ["lever"] },
      members: [],
    };
    const savedUser = { seen_at: null, saved_at: "2026-11-02T08:00:00Z", applied_at: null, hidden_at: null, has_note: false };
    const [single, grouped] = await cardsOf(api, [
      jobCard(),
      jobCard({ job_id: 7, title: "Platform Engineer", kind: "group", group, user: savedUser }),
    ]);
    const user = userEvent.setup();
    const reads = requestsTo(api, "GET /api/jobs").length;
    await user.click(within(single).getByRole("button", { name: en["card.save"] }));
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs").length).toBe(reads + 1); // refetched after the answer
    });
    const [put] = requestsTo(api, "PUT /api/jobs/5/actions");
    expect(JSON.parse(put.body ?? "null")).toEqual({ saved: true });
    expect(put.headers.get("Idempotency-Key")).toBeNull(); // the route is not keyed
    expect(put.headers.get("X-JH-Request")).toBe("1");
    await user.click(within(grouped).getByRole("button", { name: en["card.save"] }));
    await user.click(within(grouped).getByRole("button", { name: en["card.mark_applied"] }));
    await waitFor(() => {
      expect(requestsTo(api, "PUT /api/jobs/7/actions")).toHaveLength(2);
    });
    expect(bodies(api, "PUT /api/jobs/7/actions")).toEqual([
      { saved: false, apply_to_group: true }, // saved already: the toggle clears it
      { applied: true, apply_to_group: true },
    ]);
  });

  it("Hide toasts with Undo that unhides", async () => {
    const api = fakeApi({ signedIn: true });
    const shown = jobCard({ title: "Dev <img src=x onerror=alert(1)>" });
    api.routes.set("PUT /api/jobs/5/actions", (request) => {
      const body = JSON.parse(request.body ?? "{}") as { hidden?: boolean };
      api.jobs = jobsPage(body.hidden === true ? [] : [shown]); // the default list leaves hidden jobs out
      return json(actionsOut(5, { hidden_at: body.hidden === true ? NOW.toISOString() : null }));
    });
    await cardsOf(api, [shown]);
    const user = userEvent.setup();
    const card = screen.getByRole("article", { name: shown.title });
    await user.click(within(card).getByRole("button", { name: en["card.hide"] }));
    const region = screen.getByRole("region", { name: en["toast.region"] });
    expect(await within(region).findByText(`Hidden: ${shown.title}`)).toBeTruthy(); // the job, named as text
    expect(region.querySelector("img")).toBeNull();
    await waitFor(() => {
      expect(screen.queryByRole("article", { name: shown.title })).toBeNull(); // the list was read again
    });
    await user.click(within(region).getByRole("button", { name: en["action.undo"] }));
    expect(await screen.findByRole("article", { name: shown.title })).toBeTruthy();
    expect(bodies(api, "PUT /api/jobs/5/actions")).toEqual([{ hidden: true }, { hidden: false }]);
    expect(within(region).queryByText(/^Hidden: /)).toBeNull(); // the notice left with its Undo
  });

  it("a merged job toasts and refetches", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("PUT /api/jobs/5/actions", () => problem(404, "not_found", { merged_into_job_id: 9 }));
    const [card] = await cardsOf(api, [jobCard()]);
    const reads = requestsTo(api, "GET /api/jobs").length;
    await userEvent.setup().click(within(card).getByRole("button", { name: en["card.hide"] }));
    expect(await screen.findByText(en["card.merged"])).toBeTruthy();
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/jobs").length).toBe(reads + 1);
    });
    expect(screen.queryByText(/^Hidden: /)).toBeNull();
    expect(screen.queryByText(en["problem.not_found"])).toBeNull();
  });

  it("another failure toasts the translated problem", async () => {
    const api = fakeApi({ signedIn: true });
    const answers = [() => problem(422, "validation_failed"), () => problem(500, "internal")];
    api.routes.set("PUT /api/jobs/5/actions", () => answers.shift()?.() ?? problem(500, "internal"));
    const [card] = await cardsOf(api, [jobCard()]);
    const user = userEvent.setup();
    await user.click(within(card).getByRole("button", { name: en["card.save"] }));
    expect(await screen.findByText(en["problem.validation_failed"])).toBeTruthy();
    await user.click(within(card).getByRole("button", { name: en["card.mark_applied"] }));
    expect(await screen.findByText(en["problem.internal"])).toBeTruthy();
    expect(screen.queryByText(en["toast.transient"])).toBeNull(); // the action's own toast only
  });
});

describe("the card's focus", () => {
  it("has a roving tabIndex, and Enter on the card, not on its controls, opens it", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("PUT /api/jobs/5/actions", () => json(actionsOut(5, { hidden_at: NOW.toISOString() })));
    const onOpen = vi.fn();
    renderInProviders(
      <>
        <JobCard card={jobCard()} focused onOpen={onOpen} />
        <JobCard card={jobCard({ job_id: 6, title: "Second" })} focused={false} onOpen={onOpen} />
      </>,
    );
    const [first, second] = screen.getAllByRole("article");
    expect(first.tabIndex).toBe(0);
    expect(second.tabIndex).toBe(-1);
    const user = userEvent.setup();
    first.focus();
    await user.keyboard("{Enter}");
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledWith(5);
    within(first).getByRole("button", { name: en["card.hide"] }).focus();
    await user.keyboard("{Enter}");
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});
