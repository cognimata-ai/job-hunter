import { act, cleanup, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import { type FakeApi, fakeApi, json, problem, profileList, queryRow, requestsTo, settingsRead } from "../test/api";
import { formOf, renderSettings } from "../test/settings";

type ProposalOut = components["schemas"]["ProposalOut"];
type ProposedQueryOut = components["schemas"]["ProposedQueryOut"];
type LatestProposalOut = components["schemas"]["LatestProposalOut"];
type Settings = components["schemas"]["Settings"];

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const PYTHON: ProposedQueryOut = {
  text: "Senior Python Developer",
  lang: "en",
  target_title: "Backend Engineer",
  exploration: true,
  rationale: "Python is the profile's main language.",
};
const BACKEND: ProposedQueryOut = {
  text: "Backend Entwickler",
  lang: "de",
  target_title: "Backend Engineer",
  exploration: false,
  rationale: "The German title of the target role.",
};

function panel(): HTMLElement {
  return within(document.getElementById("queries") as HTMLElement).getByRole("region", {
    name: en["settings_proposal.heading"],
  });
}

async function findPanel(): Promise<HTMLElement> {
  return within(document.getElementById("queries") as HTMLElement).findByRole("region", {
    name: en["settings_proposal.heading"],
  });
}

/** The profile read with `latest` as its newest proposal; the test changes it as the server would. */
function profileWith(api: FakeApi, latest: LatestProposalOut | null, active = true): { current: LatestProposalOut | null } {
  const state = { current: latest };
  api.routes.set("GET /api/profile", () => json({ ...profileList(active), latest_proposal: state.current }));
  return state;
}

function settingsWith(api: FakeApi, changes: Partial<Settings>): void {
  const base = settingsRead();
  api.routes.set("GET /api/settings", () => json({ ...base, payload: { ...base.payload, ...changes } }));
}

/** The item of the ready proposal whose text is `text`. */
async function itemOf(text: string): Promise<HTMLElement> {
  const items = await within(await findPanel()).findAllByRole("listitem");
  const item = items.find((candidate) => within(candidate).queryByText(text) !== null);
  if (item === undefined) {
    throw new Error(`no item ${text}`);
  }
  return item;
}

describe("ProposalPanel", () => {
  it("a keyed POST then polling to ready and Add", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const api = fakeApi({ signedIn: true });
    const latest = profileWith(api, null);
    let proposal: ProposalOut = { status: "running", items: null, failure_code: null };
    api.routes.set("POST /api/profile/query-proposals", () => {
      latest.current = { id: 5, status: "running", failure_code: null };
      return json({ proposal_id: 5 }, 202);
    });
    api.routes.set("GET /api/profile/query-proposals/5", () => json(proposal));
    api.routes.set("POST /api/queries", (request) => {
      const body = JSON.parse(request.body ?? "{}") as ProposedQueryOut;
      api.queries = [queryRow({ id: 11, text: body.text, lang: body.lang, origin: "llm_proposal", enabled: false })];
      return json({ id: 11, text: body.text, lang: body.lang, origin: "llm_proposal", exploration: true, proposal_id: 5 }, 201);
    });
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    const propose = await within(await findPanel()).findByRole<HTMLButtonElement>("button", { name: "Propose queries (~$0.02)" });
    expect(propose.disabled).toBe(false);
    await user.click(propose);

    const [post] = requestsTo(api, "POST /api/profile/query-proposals");
    expect(post.headers.get("Idempotency-Key")).toMatch(UUID);
    expect(await within(panel()).findByText(en["settings_proposal.running"])).toBeTruthy();
    expect(propose.disabled).toBe(true);
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/profile/query-proposals/5")).toHaveLength(1);
    });
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/profile/query-proposals/5")).toHaveLength(2);
    });
    const profileReads = requestsTo(api, "GET /api/profile").length;
    proposal = { status: "ready", items: [PYTHON, BACKEND], failure_code: null };
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    const item = await itemOf(PYTHON.text);
    const [first, ...polls] = requestsTo(api, "GET /api/profile/query-proposals/5");
    expect(first.headers.has("X-JH-Background")).toBe(false);
    expect(polls.map((request) => request.headers.get("X-JH-Background"))).toEqual(["1", "1"]);
    // ended: the profile is read again, and the polls stop
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/profile").length).toBeGreaterThan(profileReads);
    });
    act(() => {
      vi.advanceTimersByTime(9000);
    });
    expect(requestsTo(api, "GET /api/profile/query-proposals/5")).toHaveLength(3);
    expect(propose.disabled).toBe(false);
    expect(item.textContent).toContain(PYTHON.target_title);
    expect(item.textContent).toContain(PYTHON.rationale);
    expect(item.textContent).toContain(en["settings_proposal.exploration"]);
    expect(within(item).getByText(PYTHON.text).getAttribute("lang")).toBe("en");

    await user.click(within(item).getByRole("button", { name: "Add “Senior Python Developer”" }));
    const [add] = requestsTo(api, "POST /api/queries");
    expect(JSON.parse(add.body ?? "")).toEqual({ text: PYTHON.text, lang: "en", proposal_id: 5, exploration: true });
    expect(add.headers.has("Idempotency-Key")).toBe(false);
    await waitFor(() => {
      expect(formOf(probe).draft.queries).toEqual([{ query_id: 11, enabled: true }]);
    });
    expect(await within(item).findByText(en["settings_proposal.added"])).toBeTruthy();
    expect(within(item).queryByRole("button", { name: "Add “Senior Python Developer”" })).toBeNull();
    expect(within(await itemOf(BACKEND.text)).getByRole("button", { name: "Add “Backend Entwickler”" })).toBeTruthy();
  });

  it("failure codes", async () => {
    const codes = ["budget_exhausted", "invalid_output", "refused"] as const;
    for (const code of codes) {
      const api = fakeApi({ signedIn: true });
      profileWith(api, { id: 6, status: "failed", failure_code: code });
      api.routes.set("GET /api/profile/query-proposals/6", () => json({ status: "failed", items: null, failure_code: code }));
      renderSettings(["queries"]);
      const shown = await within(await findPanel()).findByText(en[`proposal_failure.${code}`]);
      expect(shown).toBeTruthy();
      // a new proposal can be asked for
      expect(within(panel()).getByRole<HTMLButtonElement>("button", { name: "Propose queries (~$0.02)" }).disabled).toBe(false);
      cleanup();
      vi.restoreAllMocks();
    }
    // a code without a text of its own: the generic text, and the code
    const api = fakeApi({ signedIn: true });
    profileWith(api, { id: 6, status: "failed", failure_code: "llm_request_500" });
    api.routes.set("GET /api/profile/query-proposals/6", () => json({ status: "failed", items: null, failure_code: "llm_request_500" }));
    renderSettings(["queries"]);
    const code = await within(await findPanel()).findByText("llm_request_500");
    expect(code.tagName).toBe("CODE");
    expect(within(panel()).getByText(en["settings_proposal.failed"])).toBeTruthy();
  });

  it("the latest proposal after a reload", async () => {
    const api = fakeApi({ signedIn: true });
    profileWith(api, { id: 5, status: "ready", failure_code: null });
    api.routes.set("GET /api/profile/query-proposals/5", () => json({ status: "ready", items: [PYTHON, BACKEND], failure_code: null }));
    renderSettings(["queries"]);
    expect(await itemOf(PYTHON.text)).toBeTruthy();
    expect(await itemOf(BACKEND.text)).toBeTruthy();
    // and once more, as after a reload: shown again, with no second paid proposal
    cleanup();
    renderSettings(["queries"]);
    expect(await itemOf(PYTHON.text)).toBeTruthy();
    expect(requestsTo(api, "POST /api/profile/query-proposals")).toHaveLength(0);
  });

  it("the proposal button is disabled while running and without a profile", async () => {
    let api = fakeApi({ signedIn: true });
    profileWith(api, null, false);
    renderSettings(["queries"]);
    const button = await within(await findPanel()).findByRole<HTMLButtonElement>("button", { name: "Propose queries (~$0.02)" });
    expect(button.disabled).toBe(true);
    const reason = within(panel()).getByText(en["settings_proposal.no_profile"]);
    expect(button.getAttribute("aria-describedby")).toContain(reason.id);
    cleanup();
    vi.restoreAllMocks();

    api = fakeApi({ signedIn: true });
    profileWith(api, { id: 5, status: "running", failure_code: null });
    api.routes.set("GET /api/profile/query-proposals/5", () => json({ status: "running", items: null, failure_code: null }));
    renderSettings(["queries"]);
    expect(await within(await findPanel()).findByText(en["settings_proposal.running"])).toBeTruthy();
    expect(within(panel()).getByRole<HTMLButtonElement>("button", { name: "Propose queries (~$0.02)" }).disabled).toBe(true);
    expect(requestsTo(api, "POST /api/profile/query-proposals")).toHaveLength(0);
  });

  it("a proposal item already added reads Added after a reload", async () => {
    const api = fakeApi({ signedIn: true });
    profileWith(api, { id: 5, status: "ready", failure_code: null });
    api.routes.set("GET /api/profile/query-proposals/5", () => json({ status: "ready", items: [PYTHON, BACKEND], failure_code: null }));
    // the first item was added before (its text in another case); a query of the second's text is in another
    // language
    api.queries = [
      queryRow({ id: 12, text: "senior python developer", lang: "en", origin: "llm_proposal" }),
      queryRow({ id: 13, text: "Backend Entwickler", lang: "en" }),
    ];
    settingsWith(api, { queries: [{ query_id: 12, enabled: true }, { query_id: 13, enabled: true }] });
    renderSettings(["queries"]);
    const added = await itemOf(PYTHON.text);
    await waitFor(() => {
      expect(within(added).getByText(en["settings_proposal.added"])).toBeTruthy();
    });
    expect(within(added).queryByRole("button")).toBeNull();
    expect(within(await itemOf(BACKEND.text)).getByRole("button", { name: "Add “Backend Entwickler”" })).toBeTruthy();
  });

  it("an item whose query exists is enabled in the draft", async () => {
    const api = fakeApi({ signedIn: true });
    profileWith(api, { id: 5, status: "ready", failure_code: null });
    api.routes.set("GET /api/profile/query-proposals/5", () => json({ status: "ready", items: [BACKEND], failure_code: null }));
    api.queries = [queryRow({ id: 14, text: "Backend Entwickler", lang: "de", enabled: false })];
    api.routes.set("POST /api/queries", () => problem(409, "conflict_state", { query_id: 14 }));
    const { probe } = renderSettings(["queries"]);
    const user = userEvent.setup();
    await user.click(within(await itemOf(BACKEND.text)).getByRole("button", { name: "Add “Backend Entwickler”" }));
    expect(await within(panel()).findByText(en["settings_queries.exists_enabled"])).toBeTruthy();
    expect(formOf(probe).draft.queries).toEqual([{ query_id: 14, enabled: true }]);
    expect(await within(await itemOf(BACKEND.text)).findByText(en["settings_proposal.added"])).toBeTruthy();
  });
});
