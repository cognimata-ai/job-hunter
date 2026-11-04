import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import {
  type FakeApi,
  fakeApi,
  groupCard,
  groupOut,
  jobCard,
  jobsPage,
  json,
  problem,
  representativeDetail,
  requestsTo,
} from "../test/api";
import { NOW, renderApp, settled } from "../test/app";

type GroupOut = components["schemas"]["GroupOut"];

const SPLIT = "POST /api/duplicate-groups/3/split";
const INDIVIDUALLY = "POST /api/duplicate-groups/3/score-individually";
const CONFIRM = "POST /api/duplicate-groups/3/confirm";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
});

afterEach(async () => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

/** The api of a list holding the group card of job 9, and the group its detail lists now (`null` once the
 * representative has left it). */
interface Stack {
  api: FakeApi;
  group: GroupOut | null;
}

function stack(group: GroupOut = groupOut()): Stack {
  const s: Stack = { api: fakeApi({ signedIn: true }), group };
  s.api.jobs = jobsPage([groupCard()]);
  s.api.routes.set("GET /api/jobs/9", () => json(representativeDetail(s.group)));
  return s;
}

/** A write's answer: the group after it, which the representative's detail lists from then on. */
function answers(s: Stack, after: GroupOut): Response {
  s.group = after.status === "dissolved" ? null : after;
  return json(after);
}

/** The group without member `jobId`. */
function without(jobId: number, group: GroupOut = groupOut()): GroupOut {
  return { ...group, members: group.members.filter((m) => m.job_id !== jobId) };
}

/** The list at `/`, the group card expanded: its members' list. */
async function expanded() {
  renderApp("/");
  const list = await screen.findByRole("list", { name: en["jobs.title"] });
  const card = within(list).getByRole("article", { name: "Backend Engineer" });
  const user = userEvent.setup();
  await user.click(within(card).getByRole("button", { name: en["group.show_members"].replace("{{size}}", "3") }));
  const members = await within(card).findByRole("list", { name: en["group.members"] });
  return { card, members, user };
}

/** A member's item in the members' list (named by the member's title). */
function member(members: HTMLElement, title: string): HTMLElement {
  return within(members).getByRole("listitem", { name: title });
}

function names(members: HTMLElement): (string | undefined)[] {
  return within(members)
    .getAllByRole("listitem")
    .map((item) => document.getElementById(item.getAttribute("aria-labelledby") ?? "")?.textContent);
}

function bodies(api: FakeApi, call: string): unknown[] {
  return requestsTo(api, call).map((r) => (r.body === null ? null : (JSON.parse(r.body) as unknown)));
}

function disabled(button: HTMLElement): boolean {
  return (button as HTMLButtonElement).disabled;
}

describe("GroupMembers", () => {
  it("lists every member, the representative first, with its facts", async () => {
    stack();
    const { members } = await expanded();
    expect(names(members)).toEqual(["Backend Engineer", "Senior Backend Engineer", "Backend Engineer (m/w/d)"]);
    const representative = member(members, "Backend Engineer");
    expect(representative.textContent).toContain(en["group.representative"]);
    expect(representative.textContent).toContain(`${en["group.elected"]} initial`);
    expect(within(representative).getByText("initial").tagName).toBe("CODE"); // the election reason, verbatim
    const stale = member(members, "Backend Engineer (m/w/d)").textContent;
    for (const part of [
      "Arbeitnow",
      "Acme GmbH",
      "Berlin",
      "Potsdam",
      en["completeness.full"],
      en["filters.availability.stale"],
      en["target_state.deferred_duplicate"],
    ]) {
      expect(stale).toContain(part);
    }
    expect(member(members, "Senior Backend Engineer").textContent).not.toContain(en["group.representative"]);
  });

  it("Split posts and refetches the list", async () => {
    const s = stack();
    s.api.routes.set(SPLIT, () => answers(s, without(6)));
    const { members, user } = await expanded();
    const reads = requestsTo(s.api, "GET /api/jobs").length;
    await user.click(within(member(members, "Backend Engineer (m/w/d)")).getByRole("button", { name: en["group.split"] }));
    await waitFor(() => {
      expect(bodies(s.api, SPLIT)).toEqual([{ job_id: 6 }]);
    });
    const [post] = requestsTo(s.api, SPLIT);
    expect(post.headers.get("Idempotency-Key")).toBeNull(); // the route is not keyed (§14.1)
    expect(post.headers.get("X-JH-Request")).toBe("1");
    await waitFor(() => {
      expect(names(members)).toEqual(["Backend Engineer", "Senior Backend Engineer"]);
    });
    expect(document.activeElement).toBe(members); // the focused Split left with its member: focus is on the list
    await waitFor(() => {
      expect(requestsTo(s.api, "GET /api/jobs").length).toBe(reads + 1);
    });
    expect(screen.queryByText(en["group.dissolved"])).toBeNull();
  });

  it("Score individually posts", async () => {
    const s = stack();
    const after = groupOut({
      members: groupOut().members.map((m) => (m.job_id === 5 ? { ...m, score_individually: true, target_state: "pending" } : m)),
    });
    s.api.routes.set(INDIVIDUALLY, () => answers(s, after));
    const { members, user } = await expanded();
    const reads = requestsTo(s.api, "GET /api/jobs").length;
    await user.click(within(member(members, "Senior Backend Engineer")).getByRole("button", { name: en["group.score_individually"] }));
    await waitFor(() => {
      expect(bodies(s.api, INDIVIDUALLY)).toEqual([{ job_id: 5 }]);
    });
    await waitFor(() => {
      expect(within(member(members, "Senior Backend Engineer")).getByText(en["group.individual"])).toBeTruthy();
    });
    const five = member(members, "Senior Backend Engineer");
    expect(within(five).queryByRole("button", { name: en["group.score_individually"] })).toBeNull();
    expect(five.textContent).toContain(en["target_state.pending"]);
    await waitFor(() => {
      expect(requestsTo(s.api, "GET /api/jobs").length).toBe(reads + 1);
    });
  });

  it("Score individually is not offered to the representative or an already individual member", async () => {
    stack(
      groupOut({
        members: groupOut().members.map((m) => (m.job_id === 6 ? { ...m, score_individually: true, target_state: "pending" } : m)),
      }),
    );
    const { members } = await expanded();
    const offered = (title: string) =>
      within(member(members, title)).queryByRole("button", { name: en["group.score_individually"] }) !== null;
    expect([offered("Backend Engineer"), offered("Senior Backend Engineer"), offered("Backend Engineer (m/w/d)")]).toEqual([
      false,
      true,
      false,
    ]);
    // Split is on every member, the representative included
    expect(within(members).getAllByRole("button", { name: en["group.split"] })).toHaveLength(3);
    expect(within(member(members, "Backend Engineer (m/w/d)")).getByText(en["group.individual"])).toBeTruthy();
    expect(within(member(members, "Senior Backend Engineer")).queryByText(en["group.individual"])).toBeNull();
  });

  it("Confirm asks then posts and refetches", async () => {
    const s = stack();
    s.api.routes.set(CONFIRM, () => {
      s.group = null; // every other member merged into the representative: the group is gone
      return json({ job_id: 9 }, 202);
    });
    const { card, user } = await expanded();
    const reads = requestsTo(s.api, "GET /api/jobs").length;
    await user.click(within(card).getByRole("button", { name: en["group.confirm"] }));
    const dialog = await screen.findByRole("alertdialog", { name: en["group.confirm_title"] });
    expect(dialog.textContent).toContain(en["group.confirm_body"].replace("{{title}}", "Backend Engineer"));
    // cancelled: nothing is sent
    await user.click(within(dialog).getByRole("button", { name: en["action.cancel"] }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(requestsTo(s.api, CONFIRM)).toHaveLength(0);
    await user.click(within(card).getByRole("button", { name: en["group.confirm"] }));
    const again = await screen.findByRole("alertdialog", { name: en["group.confirm_title"] });
    await user.click(within(again).getByRole("button", { name: en["group.confirm"] }));
    await waitFor(() => {
      expect(bodies(s.api, CONFIRM)).toEqual([null]); // no body
    });
    expect(await screen.findByText(en["group.merged"])).toBeTruthy();
    await waitFor(() => {
      expect(requestsTo(s.api, "GET /api/jobs").length).toBe(reads + 1);
    });
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("Confirm is not offered without a representative", async () => {
    stack(
      groupOut({
        representative_job_id: null,
        unscorable_reason: "all_failed",
        members: groupOut().members.map((m) => ({ ...m, is_representative: false, target_state: "failed_transient" })),
      }),
    );
    const { card, members } = await expanded();
    expect(within(card).queryByRole("button", { name: en["group.confirm"] })).toBeNull();
    expect(within(members).queryByText(en["group.representative"])).toBeNull();
    expect(within(members).getAllByRole("button", { name: en["group.split"] })).toHaveLength(3);
    expect(within(card).getByText(en["card.not_scorable"].replace("{{reason}}", en["group_unscorable.all_failed"]))).toBeTruthy();
  });

  it("a retryable 409 offers Try again", async () => {
    const s = stack();
    s.api.routes.set(SPLIT, () =>
      requestsTo(s.api, SPLIT).length === 1 ? problem(409, "conflict_state", { retryable: true }) : answers(s, without(6)),
    );
    const { card, members, user } = await expanded();
    await user.click(within(member(members, "Backend Engineer (m/w/d)")).getByRole("button", { name: en["group.split"] }));
    expect(await within(card).findByText(en["group.conflict"])).toBeTruthy();
    expect(within(card).queryByText(en["problem.conflict_state"])).toBeNull(); // not the problem box
    await user.click(within(card).getByRole("button", { name: en["group.try_again"] }));
    await waitFor(() => {
      expect(bodies(s.api, SPLIT)).toEqual([{ job_id: 6 }, { job_id: 6 }]); // the same request again
    });
    await waitFor(() => {
      expect(names(members)).toEqual(["Backend Engineer", "Senior Backend Engineer"]);
    });
    expect(within(card).queryByText(en["group.conflict"])).toBeNull();
  });

  it("another problem shows the problem box", async () => {
    const s = stack();
    s.api.routes.set(INDIVIDUALLY, () => problem(409, "budget_exhausted"));
    const { card, members, user } = await expanded();
    await user.click(within(member(members, "Senior Backend Engineer")).getByRole("button", { name: en["group.score_individually"] }));
    const box = await within(card).findByRole("alert");
    expect(box.textContent).toContain(en["problem.budget_exhausted"]);
    expect(within(card).queryByRole("button", { name: en["group.try_again"] })).toBeNull();
    expect(within(card).queryByText(en["group.conflict"])).toBeNull();
    expect(requestsTo(s.api, INDIVIDUALLY)).toHaveLength(1);
  });

  it("a dissolved group after a split toasts and refetches", async () => {
    const s = stack(without(6));
    s.api.routes.set(SPLIT, () => {
      s.api.jobs = jobsPage([jobCard({ job_id: 9, title: "Backend Engineer" }), jobCard()]); // two plain cards
      return answers(s, groupOut({ status: "dissolved", representative_job_id: null, members: [] }));
    });
    const { members, user } = await expanded();
    const reads = requestsTo(s.api, "GET /api/jobs").length;
    await user.click(within(member(members, "Senior Backend Engineer")).getByRole("button", { name: en["group.split"] }));
    expect(await screen.findByText(en["group.dissolved"])).toBeTruthy();
    await waitFor(() => {
      expect(screen.getAllByRole("article")).toHaveLength(2);
    });
    expect(screen.queryByRole("button", { name: /^Show members/ })).toBeNull(); // no group card any more
    await settled();
    expect(requestsTo(s.api, "GET /api/jobs").length).toBe(reads + 1); // read again once
  });

  it("actions are disabled while a write runs", async () => {
    const s = stack();
    let answer: () => void = () => undefined;
    s.api.routes.set(SPLIT, () => new Promise<Response>((resolve) => { answer = () => { resolve(answers(s, without(6))); }; }));
    const { card, members, user } = await expanded();
    const actions = () => [
      ...within(members).getAllByRole("button", { name: en["group.split"] }),
      ...within(members).getAllByRole("button", { name: en["group.score_individually"] }),
      within(card).getByRole("button", { name: en["group.confirm"] }),
    ];
    expect(actions()).toHaveLength(6);
    expect(actions().some(disabled)).toBe(false);
    expect(members.getAttribute("aria-busy")).toBe("false");
    await user.click(within(member(members, "Backend Engineer (m/w/d)")).getByRole("button", { name: en["group.split"] }));
    await waitFor(() => {
      expect(members.getAttribute("aria-busy")).toBe("true");
    });
    expect(actions().every(disabled)).toBe(true);
    // a click on a disabled action sends nothing
    await user.click(within(member(members, "Senior Backend Engineer")).getByRole("button", { name: en["group.split"] }));
    expect(requestsTo(s.api, SPLIT)).toHaveLength(1);
    answer();
    await waitFor(() => {
      expect(members.getAttribute("aria-busy")).toBe("false");
    });
    expect(actions()).toHaveLength(4); // member 6's Split and Score individually have left with it
    expect(actions().some(disabled)).toBe(false);
    await settled();
  });
});
