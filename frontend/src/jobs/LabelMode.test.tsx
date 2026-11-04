import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import de from "../i18n/de.json";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import {
  detailScore,
  type FakeApi,
  fakeApi,
  jobCard,
  jobDetail,
  jobsPage,
  json,
  problem,
  requestsTo,
} from "../test/api";
import { NOW, renderApp, settled } from "../test/app";

type LabelItemOut = components["schemas"]["LabelItemOut"];
type LabellingQueueOut = components["schemas"]["LabellingQueueOut"];

// The batch's shuffle, deterministic here: the server's order unless a test sets another (`labelApi.test.ts`
// tests the random one).
const order = vi.hoisted(() => ({ of: <T,>(items: readonly T[]): T[] => [...items] }));
vi.mock("./labelApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./labelApi")>()),
  shuffle: <T,>(items: readonly T[]): T[] => order.of(items),
}));

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
  order.of = (items) => [...items];
});

afterEach(async () => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

/** An ad of the queue: job `id`, "Job <id>" at Acme in Berlin or remote, a German ad of two paragraphs. */
function item(id: number, overrides: Partial<LabelItemOut> = {}): LabelItemOut {
  return {
    job_id: id,
    title: `Job ${String(id)}`,
    company: "Acme GmbH",
    locations: ["Berlin", "Remote"],
    ad_language: "de",
    excerpt: "Wir suchen eine Backend-Entwicklerin.\nPython und PostgreSQL.",
    ...overrides,
  };
}

function queueOut(ids: number[], labelled = 23, target = 50): LabellingQueueOut {
  return { items: ids.map((id) => item(id)), progress: { labelled, target } };
}

/** `GET /api/labelling/queue` answering `state.answer`, which a test changes between reads; while `state.hold`
 * is set, a read waits until the test calls `state.release()`. */
interface QueueRoute {
  answer: LabellingQueueOut;
  hold: boolean;
  release: () => void;
}

function queueRoute(api: FakeApi, first: LabellingQueueOut): QueueRoute {
  const state: QueueRoute = { answer: first, hold: false, release: () => undefined };
  api.routes.set("GET /api/labelling/queue", () => {
    if (!state.hold) {
      return json(state.answer);
    }
    return new Promise<Response>((resolve) => {
      state.release = () => {
        state.hold = false;
        resolve(json(state.answer));
      };
    });
  });
  return state;
}

/** `POST /api/jobs/<id>/feedback` answering 201 (or `answer`'s response) for each of `ids`. */
function labelRoutes(api: FakeApi, ids: number[], answer: () => Response | Promise<Response> = () => saved()): void {
  for (const id of ids) {
    api.routes.set(`POST /api/jobs/${String(id)}/feedback`, answer);
  }
}

function saved(): Response {
  return json({ feedback_id: 80, golden_case_id: 3 }, 201);
}

function labelPosts(api: FakeApi, id: number): unknown[] {
  return requestsTo(api, `POST /api/jobs/${String(id)}/feedback`).map((request) => JSON.parse(request.body ?? "null") as unknown);
}

function allLabelPosts(api: FakeApi): string[] {
  return api.calls.filter((call) => /^POST \/api\/jobs\/\d+\/feedback$/.test(call));
}

function card(id: number): HTMLElement {
  return screen.getByRole("article", { name: `Job ${String(id)}` });
}

/** The titles of the shown cards, in order. */
function cards(): string[] {
  const queue = screen.getByRole("list", { name: en["label_mode.queue"] });
  return within(queue).getAllByRole("article").map(labelOf);
}

/** An article's accessible name: the text of the element its `aria-labelledby` names. */
function labelOf(article: HTMLElement): string {
  return document.getElementById(article.getAttribute("aria-labelledby") ?? "")?.textContent ?? "";
}

function button(scope: HTMLElement, name: string): HTMLElement {
  return within(scope).getByRole("button", { name });
}

function labelledText(label: string): string {
  return en["label_mode.labelled"].replace("{{label}}", label);
}

function progress(): HTMLElement {
  return screen.getByRole("progressbar", { name: en["label_mode.progress_label"] });
}

describe("LabelMode", () => {
  it("a card shows the ad: title, company, places and the excerpt in its language", async () => {
    const api = fakeApi({ signedIn: true });
    queueRoute(api, {
      items: [
        item(1),
        item(2, { ad_language: "other", excerpt: "<b>Mixed</b> text" }),
        item(3, { ad_language: "en", excerpt: `Intro.\n${"ü".repeat(592)} ` }), // 600 code points, cut after a space
      ],
      progress: { labelled: 0, target: 50 },
    });
    renderApp("/?label=1");
    const first = await screen.findByRole("article", { name: "Job 1" });
    expect(within(first).getByRole("heading", { name: "Job 1" })).toBeTruthy();
    expect(within(first).getByText("Acme GmbH")).toBeTruthy();
    expect(within(first).getByText("Berlin · Remote")).toBeTruthy();
    const excerpt = within(first).getByText("Wir suchen eine Backend-Entwicklerin.").parentElement;
    expect(excerpt?.getAttribute("lang")).toBe("de");
    expect(Array.from(excerpt?.querySelectorAll("p") ?? []).map((p) => p.textContent)).toEqual([
      "Wir suchen eine Backend-Entwicklerin.",
      "Python und PostgreSQL.",
    ]);
    // three large buttons, each with its key hint, and the optional comment
    const labels = within(first).getByRole("group", { name: en["feedback.label"] });
    expect(within(labels).getAllByRole("button").map((b) => [b.textContent, b.getAttribute("aria-keyshortcuts")])).toEqual([
      ["1" + en["feedback_label.good"], "1"],
      ["2" + en["feedback_label.ok"], "2"],
      ["3" + en["feedback_label.poor"], "3"],
    ]);
    expect(within(first).getByRole("textbox", { name: en["feedback.comment"] })).toBeTruthy();
    // an ad of another language has no `lang`, and its text is text
    const second = card(2);
    const other = within(second).getByText("<b>Mixed</b> text").parentElement;
    expect(other?.hasAttribute("lang")).toBe(false);
    expect(second.querySelector("b")).toBeNull();
    // an excerpt the server cut (600 code points) ends in an ellipsis; a shorter one is the whole text
    expect(card(3).querySelector("[lang] > p:last-child")?.textContent).toBe(`${"ü".repeat(592)}…`);
    expect(first.textContent).not.toContain("…");
    // the cards take part in a roving focus
    expect([first.tabIndex, second.tabIndex, card(3).tabIndex]).toEqual([0, -1, -1]);
  });

  it("the score is hidden until Reveal", async () => {
    const api = fakeApi({ signedIn: true });
    queueRoute(api, queueOut([1, 2]));
    labelRoutes(api, [1]);
    api.routes.set("GET /api/jobs/1", () => json(jobDetail({ job: { ...jobDetail().job, job_id: 1, title: "Job 1" } })));
    renderApp("/?label=1");
    const first = await screen.findByRole("article", { name: "Job 1" });
    expect(within(first).queryByRole("button", { name: en["label_mode.reveal"] })).toBeNull(); // only after a label
    const user = userEvent.setup();
    await user.click(button(first, en["feedback_label.good"]));
    const reveal = await within(first).findByRole("button", { name: en["label_mode.reveal"] });
    expect(within(first).getByText(labelledText(en["feedback_label.good"]))).toBeTruthy();
    expect(within(first).queryByRole("img", { name: /^Score/ })).toBeNull(); // still hidden after the label
    expect(reveal.getAttribute("aria-expanded")).toBe("false");
    await user.click(reveal);
    expect(await within(first).findByRole("img", { name: "Score 82, range 74 to 90" })).toBeTruthy();
    expect(reveal.getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement).toBe(reveal); // focus stays on the disclosure
    await user.click(reveal); // and it folds away again
    expect(within(first).queryByRole("img", { name: /^Score/ })).toBeNull();
  });

  it("no score or band appears before Reveal", async () => {
    const api = fakeApi({ signedIn: true });
    // the list behind Label mode holds the same jobs, scored: none of it may show
    api.jobs = jobsPage([
      jobCard({
        job_id: 1,
        title: "Job 1",
        score: { mid: "82.40", low: "74.00", high: "89.50", coverage: "0.850", confidence: "high", result_status: "accepted" },
        bucket: "scored",
      }),
    ]);
    queueRoute(api, queueOut([1, 2]));
    labelRoutes(api, [1]);
    api.routes.set("GET /api/jobs/1", () => json(jobDetail()));
    renderApp("/?label=1");
    const first = await screen.findByRole("article", { name: "Job 1" });
    await settled();
    const main = screen.getByRole("main");
    const shown = () => main.textContent;
    expect(within(main).queryByRole("img", { name: /^Score/ })).toBeNull();
    for (const text of ["82", "74", "90", "≥ 75", "60–75", "40–60", "< 40"]) {
      expect(shown(), text).not.toContain(text);
    }
    await userEvent.setup().click(button(first, en["feedback_label.ok"]));
    await within(first).findByRole("button", { name: en["label_mode.reveal"] });
    await settled();
    expect(shown()).not.toContain("82");
    expect(within(main).queryByRole("img", { name: /^Score/ })).toBeNull();
    // the detail is read only by Reveal: not on the queue's read, not after the label
    expect(api.calls.filter((call) => /^GET \/api\/jobs\/\d+$/.test(call))).toEqual([]);
  });

  it("Reveal fetches the detail score", async () => {
    const api = fakeApi({ signedIn: true });
    queueRoute(api, queueOut([1, 2]));
    labelRoutes(api, [1, 2]);
    api.routes.set("GET /api/jobs/1", () => json(jobDetail({ score: detailScore({ mid: "61.50", low: "52.00", high: "70.00" }) })));
    api.routes.set("GET /api/jobs/2", () => json(jobDetail({ score: null, bucket: null })));
    renderApp("/?label=1");
    const first = await screen.findByRole("article", { name: "Job 1" });
    const user = userEvent.setup();
    await user.click(button(first, en["feedback_label.good"]));
    await user.click(await within(first).findByRole("button", { name: en["label_mode.reveal"] }));
    expect(await within(first).findByRole("img", { name: "Score 62, range 52 to 70" })).toBeTruthy();
    expect(requestsTo(api, "GET /api/jobs/1")).toHaveLength(1);
    // Reveal only reads: no `seen_on_open`, no other write
    expect(api.calls.filter((call) => call.startsWith("PUT "))).toEqual([]);
    // a job without a published score says so
    const second = card(2);
    await user.click(button(second, en["feedback_label.poor"]));
    await user.click(await within(second).findByRole("button", { name: en["label_mode.reveal"] }));
    expect(await within(second).findByText(en["label_mode.no_score"])).toBeTruthy();
    expect(requestsTo(api, "GET /api/jobs/2")).toHaveLength(1);
  });

  it("keys 1, 2 and 3 label the focused card", async () => {
    const api = fakeApi({ signedIn: true });
    queueRoute(api, queueOut([1, 2, 3]));
    labelRoutes(api, [1, 2, 3]);
    renderApp("/?label=1");
    await screen.findByRole("article", { name: "Job 3" });
    const user = userEvent.setup();
    await user.keyboard("j"); // from outside the queue: to its current card
    expect(document.activeElement).toBe(card(1));
    await user.keyboard("1");
    await within(card(1)).findByText(labelledText(en["feedback_label.good"]));
    expect(document.activeElement).toBe(card(2)); // on to the next card to label
    await user.keyboard("2");
    await within(card(2)).findByText(labelledText(en["feedback_label.ok"]));
    expect(document.activeElement).toBe(card(3));
    await user.keyboard("k"); // j and k move between the cards
    expect(document.activeElement).toBe(card(2));
    await user.keyboard("j");
    // a key on one of the card's buttons labels it too
    within(card(3)).getByRole("button", { name: en["feedback_label.good"] }).focus();
    await user.keyboard("3");
    await within(card(3)).findByText(labelledText(en["feedback_label.poor"]));
    expect(labelPosts(api, 1)).toEqual([{ kind: "label", label: "good" }]);
    expect(labelPosts(api, 2)).toEqual([{ kind: "label", label: "ok" }]);
    expect(labelPosts(api, 3)).toEqual([{ kind: "label", label: "poor" }]);
    // a label is no keyed route (§14.1): no Idempotency-Key
    expect(api.requests.filter((r) => r.method === "POST" && r.headers.has("Idempotency-Key"))).toEqual([]);
  });

  it("keys typed in the comment do not label", async () => {
    const api = fakeApi({ signedIn: true });
    queueRoute(api, queueOut([1, 2]));
    labelRoutes(api, [1, 2]);
    renderApp("/?label=1");
    const first = await screen.findByRole("article", { name: "Job 1" });
    const user = userEvent.setup();
    const comment = within(first).getByRole<HTMLTextAreaElement>("textbox", { name: en["feedback.comment"] });
    await user.click(comment);
    await user.keyboard("1 2 3 jk");
    expect(comment.value).toBe("1 2 3 jk");
    expect(document.activeElement).toBe(comment);
    // with a modifier held, or with focus outside the cards, a digit labels nothing either
    first.focus();
    await user.keyboard("{Control>}1{/Control}{Alt>}2{/Alt}{Meta>}3{/Meta}");
    screen.getByRole("button", { name: en["label_mode.next_batch"] }).focus();
    await user.keyboard("1");
    await settled();
    expect(allLabelPosts(api)).toEqual([]);
    expect(within(first).getByRole("button", { name: en["feedback_label.good"] })).toBeTruthy(); // still to label
    // the comment goes with the card's label, trimmed
    fireEvent.change(comment, { target: { value: "  Too far away  " } });
    await user.click(button(first, en["feedback_label.poor"]));
    await within(first).findByText(labelledText(en["feedback_label.poor"]));
    expect(labelPosts(api, 1)).toEqual([{ kind: "label", label: "poor", comment: "Too far away" }]);
  });

  it("a comment over 2,000 code points blocks the label, and the buttons are disabled while it saves", async () => {
    const api = fakeApi({ signedIn: true });
    queueRoute(api, queueOut([1, 2]));
    let finish: (() => void) | null = null;
    labelRoutes(api, [1], () => new Promise<Response>((resolve) => {
      finish = () => { resolve(saved()); };
    }));
    renderApp("/?label=1");
    const first = await screen.findByRole("article", { name: "Job 1" });
    const comment = within(first).getByRole<HTMLTextAreaElement>("textbox", { name: en["feedback.comment"] });
    fireEvent.change(comment, { target: { value: "😀".repeat(2001) } });
    expect(within(first).getByText("2,001 / 2,000")).toBeTruthy();
    expect(within(first).getByText(en["feedback.too_long"].replace("{{max}}", "2,000"))).toBeTruthy();
    const good = button(first, en["feedback_label.good"]);
    expect(good.getAttribute("aria-disabled")).toBe("true");
    const user = userEvent.setup();
    await user.click(good);
    first.focus();
    await user.keyboard("1");
    await settled();
    expect(allLabelPosts(api)).toEqual([]);
    fireEvent.change(comment, { target: { value: "😀".repeat(2000) } }); // 4,000 UTF-16 units: allowed
    expect(good.getAttribute("aria-disabled")).toBe("false");
    await user.click(good);
    await waitFor(() => {
      expect(allLabelPosts(api)).toHaveLength(1);
    });
    const labels = within(first).getByRole("group", { name: en["feedback.label"] });
    expect(within(labels).getAllByRole("button").map((b) => b.getAttribute("aria-disabled"))).toEqual(["true", "true", "true"]);
    expect(document.activeElement).toBe(good); // focus stays on the pressed button while it saves
    await user.keyboard("2"); // and a second label waits for the first
    await user.click(button(first, en["feedback_label.poor"]));
    expect(allLabelPosts(api)).toHaveLength(1);
    await act(async () => {
      finish?.();
      await Promise.resolve();
    });
    expect(await within(first).findByText(labelledText(en["feedback_label.good"]))).toBeTruthy();
    expect(allLabelPosts(api)).toHaveLength(1);
  });

  it("the shown order is the batch's shuffle, not the server's, and a read never reorders it", async () => {
    const api = fakeApi({ signedIn: true });
    const queue = queueRoute(api, queueOut([1, 2, 3]));
    labelRoutes(api, [3]);
    const shuffled: number[][] = [];
    order.of = <T,>(items: readonly T[]): T[] => {
      shuffled.push(items.map((one) => (one as LabelItemOut).job_id));
      return [...items].reverse();
    };
    renderApp("/?label=1");
    await screen.findByRole("article", { name: "Job 1" });
    expect(cards()).toEqual(["Job 3", "Job 2", "Job 1"]); // the shuffle's order, not the server's 1, 2, 3
    expect(shuffled).toEqual([[1, 2, 3]]); // once, when the batch was taken
    queue.answer = queueOut([4, 5, 3], 24);
    await userEvent.setup().click(button(card(3), en["feedback_label.good"]));
    await waitFor(() => {
      expect(progress().textContent).toBe("24 / 50 labelled");
    });
    expect(cards()).toEqual(["Job 3", "Job 2", "Job 1"]);
    expect(shuffled).toHaveLength(1); // the read after the label moved only the progress
    // Next batch shuffles the answer it takes; the job labelled in this session goes last
    await userEvent.setup().click(screen.getByRole("button", { name: en["label_mode.next_batch"] }));
    expect(shuffled).toEqual([[1, 2, 3], [4, 5, 3]]);
    expect(cards()).toEqual(["Job 5", "Job 4", "Job 3"]);
  });

  it("an empty batch takes the next answer with ads: Check again", async () => {
    const api = fakeApi({ signedIn: true });
    const queue = queueRoute(api, queueOut([], 0));
    renderApp("/?label=1");
    const again = await screen.findByRole("button", { name: en["label_mode.check_again"] });
    expect(screen.getByText(en["label_mode.empty"])).toBeTruthy();
    queue.answer = queueOut([4, 5], 0);
    queue.hold = true;
    await userEvent.setup().click(again);
    expect(again.textContent).toBe(en["label_mode.checking"]);
    expect(again.getAttribute("aria-disabled")).toBe("true");
    await act(async () => {
      queue.release();
      await Promise.resolve();
    });
    expect(await screen.findByRole("article", { name: "Job 4" })).toBeTruthy();
    expect(cards()).toEqual(["Job 4", "Job 5"]);
    expect(document.activeElement).toBe(card(4)); // Check again went with the empty state
    const reads = requestsTo(api, "GET /api/labelling/queue");
    expect(reads).toHaveLength(2);
    expect(reads[1].headers.has("X-JH-Background")).toBe(false); // the user's own read, not a poll
  });

  it("an empty batch takes the next answer with ads: the 60 s background read", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"], now: NOW });
    const api = fakeApi({ signedIn: true });
    const queue = queueRoute(api, queueOut([], 0));
    renderApp("/?label=1");
    expect(await screen.findByText(en["label_mode.empty"])).toBeTruthy();
    await settled();
    const before = requestsTo(api, "GET /api/labelling/queue").length;
    act(() => {
      vi.advanceTimersByTime(59_000);
    });
    expect(requestsTo(api, "GET /api/labelling/queue")).toHaveLength(before); // not before the minute
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/labelling/queue")).toHaveLength(before + 1);
    });
    expect(requestsTo(api, "GET /api/labelling/queue").at(-1)?.headers.get("X-JH-Background")).toBe("1");
    queue.answer = queueOut([6], 0);
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(await screen.findByRole("article", { name: "Job 6" })).toBeTruthy();
    // a batch with ads is not polled
    const reads = requestsTo(api, "GET /api/labelling/queue").length;
    act(() => {
      vi.advanceTimersByTime(180_000);
    });
    await settled();
    expect(requestsTo(api, "GET /api/labelling/queue")).toHaveLength(reads);
  });

  it("the progress follows the refetched answer", async () => {
    const api = fakeApi({ signedIn: true });
    const queue = queueRoute(api, queueOut([1, 2, 3], 23));
    labelRoutes(api, [1]);
    renderApp("/?label=1");
    await screen.findByRole("article", { name: "Job 1" });
    expect(progress().textContent).toBe("23 / 50 labelled");
    expect(progress().getAttribute("aria-valuenow")).toBe("23");
    const reads = requestsTo(api, "GET /api/labelling/queue");
    expect(reads).toHaveLength(1);
    expect(reads[0].search.toString()).toBe("limit=10");
    queue.answer = queueOut([2, 3, 4], 24);
    await userEvent.setup().click(button(card(1), en["feedback_label.good"]));
    await waitFor(() => {
      expect(progress().textContent).toBe("24 / 50 labelled");
    });
    expect(progress().getAttribute("aria-valuenow")).toBe("24");
    expect(requestsTo(api, "GET /api/labelling/queue")).toHaveLength(2); // the label read it again, once
  });

  it("the batch does not reshuffle after a label", async () => {
    const api = fakeApi({ signedIn: true });
    const queue = queueRoute(api, queueOut([1, 2, 3]));
    labelRoutes(api, [1]);
    renderApp("/?label=1");
    await screen.findByRole("article", { name: "Job 1" });
    expect(cards()).toEqual(["Job 1", "Job 2", "Job 3"]);
    queue.answer = queueOut([3, 2, 4], 24); // the server's next answer: a new order and a new job
    await userEvent.setup().click(button(card(1), en["feedback_label.good"]));
    await waitFor(() => {
      expect(progress().textContent).toBe("24 / 50 labelled");
    });
    await settled();
    expect(cards()).toEqual(["Job 1", "Job 2", "Job 3"]); // the cards stay where they were
    expect(within(card(1)).getByText(labelledText(en["feedback_label.good"]))).toBeTruthy();
    expect(within(card(1)).getByRole("button", { name: en["label_mode.change"] })).toBeTruthy();
    expect(screen.queryByRole("article", { name: "Job 4" })).toBeNull();
  });

  it("Next batch replaces the cards and focuses the first", async () => {
    const api = fakeApi({ signedIn: true });
    const queue = queueRoute(api, queueOut([1, 2, 3]));
    labelRoutes(api, [1]);
    renderApp("/?label=1");
    await screen.findByRole("article", { name: "Job 1" });
    // the server fills a batch with labelled jobs once the unlabelled ones run out
    queue.answer = queueOut([4, 5, 1], 24);
    const user = userEvent.setup();
    await user.click(button(card(1), en["feedback_label.good"]));
    await waitFor(() => {
      expect(progress().textContent).toBe("24 / 50 labelled");
    });
    await user.click(screen.getByRole("button", { name: en["label_mode.next_batch"] }));
    expect(cards()).toEqual(["Job 4", "Job 5", "Job 1"]);
    expect(document.activeElement).toBe(card(4));
    // a job labelled in this session keeps its label in the new batch
    expect(within(card(1)).getByText(labelledText(en["feedback_label.good"]))).toBeTruthy();
    expect(within(card(4)).getByRole("button", { name: en["feedback_label.good"] })).toBeTruthy();
    expect(requestsTo(api, "GET /api/labelling/queue")).toHaveLength(2); // the latest answer, not a new read
  });

  it("after a label focus moves to the next unlabelled card", async () => {
    const api = fakeApi({ signedIn: true });
    const queue = queueRoute(api, queueOut([1, 2, 3]));
    labelRoutes(api, [1, 2, 3]);
    renderApp("/?label=1");
    await screen.findByRole("article", { name: "Job 1" });
    const user = userEvent.setup();
    await user.click(button(card(2), en["feedback_label.ok"]));
    await within(card(2)).findByText(labelledText(en["feedback_label.ok"]));
    expect(document.activeElement).toBe(card(3));
    await user.keyboard("1");
    await within(card(3)).findByText(labelledText(en["feedback_label.good"]));
    expect(document.activeElement).toBe(card(1)); // after the last card, round to the first one left
    // the batch's last label: the cards stay, "Batch done" says so, and Next batch has focus
    api.routes.set("GET /api/jobs/1", () => json(jobDetail()));
    const status = screen.getByText((_, element) => element?.getAttribute("role") === "status" && element.tagName === "P");
    expect(status.textContent).toBe(""); // rendered before it speaks
    queue.answer = queueOut([6, 7], 26);
    queue.hold = true; // the read that follows the last label is still running
    await user.keyboard("3");
    await within(card(1)).findByText(labelledText(en["feedback_label.poor"]));
    const next = screen.getByRole("button", { name: en["label_mode.next_batch"] });
    expect(document.activeElement).toBe(next);
    expect(status.textContent).toBe(en["label_mode.batch_done"]);
    expect(cards()).toEqual(["Job 1", "Job 2", "Job 3"]);
    // the last card's Reveal score and Change label still work
    await user.click(button(card(1), en["label_mode.reveal"]));
    expect(await within(card(1)).findByRole("img", { name: "Score 82, range 74 to 90" })).toBeTruthy();
    await user.click(button(card(1), en["label_mode.change"]));
    expect(button(card(1), en["feedback_label.poor"]).getAttribute("aria-pressed")).toBe("true");
    await user.click(button(card(1), en["action.cancel"]));
    // Next batch, pressed while that read runs, takes its answer once it is in
    await user.click(next);
    expect(next.getAttribute("aria-busy")).toBe("true");
    expect(cards()).toEqual(["Job 1", "Job 2", "Job 3"]);
    await act(async () => {
      queue.release();
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(cards()).toEqual(["Job 6", "Job 7"]);
    });
    expect(document.activeElement).toBe(card(6));
    expect(progress().textContent).toBe("26 / 50 labelled");
    expect(screen.getByRole("button", { name: en["label_mode.next_batch"] }).getAttribute("aria-busy")).toBe("false");
    expect(status.textContent).toBe(""); // a new batch, not done
    expect(requestsTo(api, "GET /api/labelling/queue")).toHaveLength(4); // the first and one after each label
  });

  it("nothing replaces a done batch but Next batch", async () => {
    const api = fakeApi({ signedIn: true });
    const queue = queueRoute(api, queueOut([1]));
    labelRoutes(api, [1]);
    renderApp("/?label=1");
    await screen.findByRole("article", { name: "Job 1" });
    queue.answer = queueOut([8, 9], 24);
    await userEvent.setup().click(button(card(1), en["feedback_label.good"]));
    await waitFor(() => {
      expect(progress().textContent).toBe("24 / 50 labelled");
    });
    await settled();
    expect(cards()).toEqual(["Job 1"]);
    expect(screen.getByText(en["label_mode.batch_done"])).toBeTruthy();
    await userEvent.setup().click(screen.getByRole("button", { name: en["label_mode.next_batch"] }));
    expect(cards()).toEqual(["Job 8", "Job 9"]); // no read is running: the latest answer, at once
    expect(document.activeElement).toBe(card(8));
  });

  it("Change label re-posts the card's label, and Cancel keeps it", async () => {
    const api = fakeApi({ signedIn: true });
    queueRoute(api, queueOut([1, 2]));
    labelRoutes(api, [1]);
    renderApp("/?label=1");
    const first = await screen.findByRole("article", { name: "Job 1" });
    const user = userEvent.setup();
    await user.type(within(first).getByRole("textbox", { name: en["feedback.comment"] }), "Nice team");
    await user.click(button(first, en["feedback_label.good"]));
    await user.click(await within(first).findByRole("button", { name: en["label_mode.change"] }));
    // the three buttons again, the label given pressed and focused, the comment kept
    const good = button(first, en["feedback_label.good"]);
    expect(good.getAttribute("aria-pressed")).toBe("true");
    expect(button(first, en["feedback_label.ok"]).getAttribute("aria-pressed")).toBe("false");
    expect(document.activeElement).toBe(good);
    expect(within(first).getByRole<HTMLTextAreaElement>("textbox", { name: en["feedback.comment"] }).value).toBe("Nice team");
    await user.click(button(first, en["action.cancel"]));
    expect(within(first).getByText(labelledText(en["feedback_label.good"]))).toBeTruthy();
    expect(document.activeElement).toBe(button(first, en["label_mode.change"]));
    expect(labelPosts(api, 1)).toHaveLength(1);
    await user.click(button(first, en["label_mode.change"]));
    await user.click(button(first, en["feedback_label.poor"]));
    await within(first).findByText(labelledText(en["feedback_label.poor"]));
    expect(labelPosts(api, 1)).toEqual([
      { kind: "label", label: "good", comment: "Nice team" },
      { kind: "label", label: "poor", comment: "Nice team" }, // the server upserts the case
    ]);
  });

  it("409 shows the no-score message", async () => {
    const api = fakeApi({ signedIn: true });
    queueRoute(api, queueOut([1, 2]));
    labelRoutes(api, [1], () => problem(409, "conflict_state"));
    renderApp("/?label=1");
    const first = await screen.findByRole("article", { name: "Job 1" });
    const user = userEvent.setup();
    await user.click(button(first, en["feedback_label.ok"]));
    const alert = await within(first).findByRole("alert");
    expect(alert.textContent).toBe(en["feedback.no_score"]);
    // the card stays to label: its buttons, no Reveal, no label counted
    expect(within(first).getByRole("button", { name: en["feedback_label.ok"] }).getAttribute("aria-disabled")).toBe("false");
    expect(within(first).queryByRole("button", { name: en["label_mode.reveal"] })).toBeNull();
    expect(within(first).queryByText(labelledText(en["feedback_label.ok"]))).toBeNull();
    expect(requestsTo(api, "GET /api/labelling/queue")).toHaveLength(1);
    expect(progress().textContent).toBe("23 / 50 labelled");
    expect(screen.queryByText(en["toast.transient"])).toBeNull();
  });

  it("any other problem shows a problem box with Retry, which posts the label again", async () => {
    const api = fakeApi({ signedIn: true });
    queueRoute(api, queueOut([1, 2]));
    let down = true;
    labelRoutes(api, [1], () => (down ? problem(500, "internal") : saved()));
    renderApp("/?label=1");
    const first = await screen.findByRole("article", { name: "Job 1" });
    const user = userEvent.setup();
    await user.click(button(first, en["feedback_label.poor"]));
    const box = await within(first).findByRole("alert");
    expect(box.textContent).toContain(en["problem.internal"]);
    expect(screen.queryByText(en["toast.transient"])).toBeNull(); // the card tells it, no toast besides
    down = false;
    await user.click(within(box).getByRole("button", { name: en["action.retry"] }));
    expect(await within(first).findByText(labelledText(en["feedback_label.poor"]))).toBeTruthy();
    expect(labelPosts(api, 1)).toEqual([{ kind: "label", label: "poor" }, { kind: "label", label: "poor" }]);
  });

  it("the progressbar has its aria values", async () => {
    const api = fakeApi({ signedIn: true });
    const queue = queueRoute(api, queueOut([1], 23));
    renderApp("/?label=1");
    await screen.findByRole("article", { name: "Job 1" });
    const bar = progress();
    expect(bar.getAttribute("aria-valuenow")).toBe("23");
    expect(bar.getAttribute("aria-valuemin")).toBe("0");
    expect(bar.getAttribute("aria-valuemax")).toBe("50");
    expect(bar.textContent).toBe("23 / 50 labelled");
    expect(bar.getAttribute("aria-valuetext")).toBe("23 / 50 labelled");
    // the fill is an SVG rect's width, never a style
    expect(bar.querySelector("rect[data-part=fill]")?.getAttribute("width")).toBe("46");
    expect(screen.getByRole("region", { name: en["label_mode.title"] }).querySelectorAll("[style]")).toHaveLength(0);
    cleanup();
    // at or past the target, the text says so; the value stays within its range
    queue.answer = queueOut([1], 52);
    renderApp("/?label=1");
    await screen.findByRole("article", { name: "Job 1" });
    expect(progress().textContent).toBe("52 / 50 labelled — target reached");
    expect(progress().getAttribute("aria-valuenow")).toBe("50");
    expect(progress().getAttribute("aria-valuetext")).toBe("52 / 50 labelled — target reached"); // the real count
    expect(progress().querySelector("rect[data-part=fill]")?.getAttribute("width")).toBe("100");
    cleanup();
    // the numbers are the UI language's
    api.uiLanguage = "de";
    queue.answer = { items: [item(1)], progress: { labelled: 1234, target: 2000 } };
    renderApp("/?label=1");
    await waitFor(() => {
      expect(screen.getByRole("progressbar", { name: de["label_mode.progress_label"] }).textContent).toBe("1.234 / 2.000 beurteilt");
    });
  });

  it("loading, error and empty states", async () => {
    const api = fakeApi({ signedIn: true });
    let release: (() => void) | null = null;
    api.routes.set("GET /api/labelling/queue", () =>
      new Promise<Response>((resolve) => {
        release = () => { resolve(json(queueOut([1]))); };
      }),
    );
    renderApp("/?label=1");
    expect(await screen.findByText(en["label_mode.ignores_filters"])).toBeTruthy();
    const loading = await within(screen.getByRole("main")).findByRole("status", { busy: true });
    expect(loading.querySelectorAll("[aria-hidden=true]")).toHaveLength(3); // three skeleton cards
    await act(async () => {
      release?.();
      await Promise.resolve();
    });
    expect(await screen.findByRole("article", { name: "Job 1" })).toBeTruthy();
    cleanup();

    // a failed read: the problem with Retry
    api.routes.set("GET /api/labelling/queue", () => problem(422, "validation_failed"));
    renderApp("/?label=1");
    const box = await screen.findByRole("alert");
    expect(box.textContent).toContain(en["problem.validation_failed"]);
    queueRoute(api, queueOut([1]));
    await userEvent.setup().click(within(box).getByRole("button", { name: en["action.retry"] }));
    expect(await screen.findByRole("article", { name: "Job 1" })).toBeTruthy();
    cleanup();

    // an empty queue with a profile: nothing scored to label yet
    queueRoute(api, queueOut([], 0));
    renderApp("/?label=1");
    expect(await screen.findByText(en["label_mode.empty"])).toBeTruthy();
    expect(screen.queryByRole("link", { name: en["jobs.no_profile"] })).toBeNull();
    expect(screen.queryByRole("button", { name: en["label_mode.next_batch"] })).toBeNull();
  });

  it("an empty queue without a profile shows the no-profile notice", async () => {
    const api = fakeApi({ signedIn: true });
    api.profileActive = false;
    queueRoute(api, queueOut([], 0));
    renderApp("/?label=1");
    const notice = await screen.findByRole("link", { name: en["jobs.no_profile"] });
    expect(notice.getAttribute("href")).toBe("/settings#cv");
    expect(progress().textContent).toBe("0 / 50 labelled");
    await settled();
    expect(screen.queryByText(en["label_mode.empty"])).toBeNull();
    expect(screen.queryByRole("article")).toBeNull();
  });

  it("no drawer shows over Label mode, and entering it drops the drawer's job", async () => {
    const api = fakeApi({ signedIn: true });
    api.jobs = jobsPage([jobCard({ job_id: 5, title: "Job 5" })]);
    queueRoute(api, queueOut([1]));
    api.routes.set("GET /api/jobs/5", () => json(jobDetail()));
    // a pasted URL with both: Label mode, no drawer, the detail not read
    renderApp("/?label=1&job=5");
    await screen.findByRole("article", { name: "Job 1" });
    await settled();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(requestsTo(api, "GET /api/jobs/5")).toHaveLength(0);
    cleanup();
    // the drawer open, then Label mode entered: the drawer's job goes with it
    const app = renderApp("/?job=5");
    expect(await screen.findByRole("dialog")).toBeTruthy();
    const panel = screen.getByRole("complementary", { name: en["filters.title"], hidden: true });
    fireEvent.click(within(panel).getByRole("button", { name: en["filters.label_mode"], hidden: true }));
    await waitFor(() => {
      expect(app.router.state.location.searchStr).toBe("?label=1");
    });
    expect(await screen.findByRole("article", { name: "Job 1" })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("label=1 shows Label mode in the list's place, and leaving it shows the list as it was", async () => {
    const api = fakeApi({ signedIn: true });
    api.jobs = jobsPage([jobCard({ job_id: 7, title: "Job 7" }), jobCard({ job_id: 8, title: "Job 8" })]);
    queueRoute(api, queueOut([1]));
    const app = renderApp("/?sort=posted");
    await screen.findByRole("article", { name: "Job 8" });
    const panel = screen.getByRole("complementary", { name: en["filters.title"] });
    const toggle = within(panel).getByRole("button", { name: en["filters.label_mode"] });
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    const listReads = requestsTo(api, "GET /api/jobs").length;
    const user = userEvent.setup();
    await user.click(toggle);
    expect(await screen.findByRole("article", { name: "Job 1" })).toBeTruthy();
    expect(app.router.state.location.searchStr).toBe("?sort=posted&label=1");
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText(en["label_mode.ignores_filters"])).toBeTruthy();
    expect(screen.queryByRole("article", { name: "Job 7" })).toBeNull();
    expect(screen.queryByRole("combobox", { name: en["jobs.sort"] })).toBeNull(); // the list's toolbar went with it
    expect(document.activeElement).toBe(toggle); // the panel stays, and its toggle keeps focus
    // the toggle again: the list as it was, not read again
    await user.click(toggle);
    expect(await screen.findByRole("article", { name: "Job 7" })).toBeTruthy();
    expect(screen.queryByRole("article", { name: "Job 1" })).toBeNull();
    expect(app.router.state.location.searchStr).toBe("?sort=posted");
    expect(requestsTo(api, "GET /api/jobs")).toHaveLength(listReads);
    // Back leaves Label mode too; the focus that was in it goes to the page's heading
    await user.click(toggle);
    await screen.findByRole("article", { name: "Job 1" });
    await user.keyboard("j");
    expect(document.activeElement).toBe(card(1));
    act(() => {
      app.router.history.back();
    });
    expect(await screen.findByRole("article", { name: "Job 7" })).toBeTruthy();
    expect(app.router.state.location.searchStr).toBe("?sort=posted");
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: en["jobs.title"] }));
    expect(requestsTo(api, "GET /api/jobs")).toHaveLength(listReads);
    // the panel's Reset in Label mode keeps it, and the queue is not read again: it ignores the filters
    await user.click(toggle);
    await screen.findByRole("article", { name: "Job 1" });
    await user.click(within(panel).getByRole("button", { name: en["filters.reset"] }));
    await waitFor(() => {
      expect(app.router.state.location.searchStr).toBe("?label=1");
    });
    expect(screen.getByRole("article", { name: "Job 1" })).toBeTruthy();
    expect(requestsTo(api, "GET /api/labelling/queue")).toHaveLength(1);
  });
});
