import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../../api/schema";
import en from "../../i18n/en.json";
import { type FakeApi, fakeApi, json, problem, requestsTo } from "../../test/api";
import { renderInProviders, settled } from "../../test/app";
import { type ReviewShape, reviewDraft, sentEdits } from "../../test/fixtures/drafts";
import { type DraftOut, useDraft } from "./api";
import { TextReview } from "./TextReview";

type SpanEditIn = components["schemas"]["SpanEditIn"];

afterEach(() => {
  vi.restoreAllMocks();
  window.getSelection()?.removeAllRanges();
});

/** The review of draft 7 as the section reads it, so each edit's read of the draft shows the next state. */
function Harness() {
  const draft = useDraft(7);
  return draft.data === undefined ? null : <TextReview draftId={7} draft={draft.data} extractUsd="0.140000" />;
}

/** Draft 7 in the states of `shape`: a GET answers the current one, each PUT moves to the next. */
function review(api: FakeApi, shape: ReviewShape, start = 0): { puts: () => SpanEditIn[][] } {
  let at = start;
  api.routes.set("GET /api/profile/drafts/7", () => json(reviewDraft(shape, at)));
  api.routes.set("PUT /api/profile/drafts/7/text", () => {
    at += 1;
    return json({ stripped_text_preview: reviewDraft(shape, at).stripped_text_preview });
  });
  return {
    puts: () =>
      requestsTo(api, "PUT /api/profile/drafts/7/text").map((r) => (JSON.parse(r.body ?? "{}") as { span_edits: SpanEditIn[] }).span_edits),
  };
}

function text(): HTMLElement {
  return screen.getByTestId("cv-text");
}

/** The preview's text nodes under the review's text, in order (its buttons and hidden labels are not text). */
function previewNodes(): Text[] {
  const nodes: Text[] = [];
  const walker = document.createTreeWalker(text(), NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    if (node.parentElement?.closest("[data-preview]") !== null) {
      nodes.push(node as Text);
    }
  }
  return nodes;
}

/** Selects `[from, to)` of the review's text: each a needle and the offset after its start (UTF-16). */
function select(from: { needle: string; at?: number }, to: { needle: string; at?: number }) {
  const find = (needle: string) => {
    const node = previewNodes().find((n) => n.data.includes(needle));
    if (node === undefined) {
      throw new Error(`no text node holds ${needle}`);
    }
    return node;
  };
  const start = find(from.needle);
  const end = find(to.needle);
  const range = document.createRange();
  range.setStart(start, start.data.indexOf(from.needle) + (from.at ?? 0));
  range.setEnd(end, end.data.indexOf(to.needle) + (to.at ?? to.needle.length));
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
  act(() => {
    document.dispatchEvent(new Event("selectionchange"));
  });
}

/** Selects `needle` in the review's text. */
function selectText(needle: string) {
  select({ needle }, { needle });
}

function markButton(): HTMLButtonElement {
  return screen.getByRole<HTMLButtonElement>("button", { name: en["cv.text.mark"] });
}

async function mark(): Promise<void> {
  // the button keeps the selection: its mousedown does not move it
  expect(fireEvent.mouseDown(markButton())).toBe(false);
  fireEvent.click(markButton());
  await settled();
}

/** The preview text the review shows: its preview text nodes, without its buttons. */
function shown(): string {
  return previewNodes().map((node) => node.data).join("");
}

async function shows(preview: string): Promise<void> {
  await waitFor(() => {
    expect(shown()).toBe(preview);
  });
}

describe("TextReview", () => {
  it("marks a missed hyphen-glued phone value", async () => {
    const api = fakeApi({ signedIn: true });
    const { puts } = review(api, "hyphen_phone");
    renderInProviders(<Harness />);
    await shows(reviewDraft("hyphen_phone").stripped_text_preview ?? "");
    expect(screen.getByText("Removed: 1 name · 1 e-mail")).toBeTruthy();
    expect(markButton().disabled).toBe(true);
    expect(screen.getByText(en["cv.text.select_first"])).toBeTruthy();
    selectText("0-170-1234567");
    expect(markButton().disabled).toBe(false);
    await mark();
    expect(puts()).toEqual([sentEdits("hyphen_phone", 1)]);
    expect(puts()[0]).toEqual([{ op: "remove", start: 46, end: 59 }]);
    await shows("[NAME]\nSenior Backend Engineer\nMobil: [REMOVED]\nE-Mail: [EMAIL]");
    // the edit read the draft again
    expect(requestsTo(api, "GET /api/profile/drafts/7")).toHaveLength(2);
    expect(requestsTo(api, "PUT /api/profile/drafts/7/text")[0].headers.has("Idempotency-Key")).toBe(false);
  });

  it("marks a missed Leiter or Kaufmann surname", async () => {
    const api = fakeApi({ signedIn: true });
    const { puts } = review(api, "kaufmann_surname");
    renderInProviders(<Harness />);
    await shows(reviewDraft("kaufmann_surname").stripped_text_preview ?? "");
    selectText("Kaufmann, Erika");
    await mark();
    expect(puts()).toEqual([sentEdits("kaufmann_surname", 1)]);
    await shows("[REMOVED]\nSenior Backend Engineer\n[EMAIL]");
  });

  it("marks a Name: <title> line", async () => {
    const api = fakeApi({ signedIn: true });
    const { puts } = review(api, "name_title_line");
    renderInProviders(<Harness />);
    await shows(reviewDraft("name_title_line").stripped_text_preview ?? "");
    selectText("Max Mustermann");
    await mark();
    expect(puts()).toEqual([[{ op: "remove", start: 17, end: 31 }]]);
    expect(puts()).toEqual([sentEdits("name_title_line", 1)]);
    await shows("Lebenslauf\nName: [REMOVED], Senior Backend Engineer\nTelefon: [PHONE]");
  });

  it("marks a glued date of birth", async () => {
    const api = fakeApi({ signedIn: true });
    const { puts } = review(api, "glued_dob");
    renderInProviders(<Harness />);
    await shows(reviewDraft("glued_dob").stripped_text_preview ?? "");
    selectText("01.02.1990");
    await mark();
    expect(puts()).toEqual([sentEdits("glued_dob", 1)]);
    await shows("[NAME]\nGeburtsdatum[REMOVED]\nWohnort Berlin");
  });

  it("restores Berlin Germany wrongly replaced by [NAME]", async () => {
    const api = fakeApi({ signedIn: true });
    const { puts } = review(api, "berlin_germany");
    renderInProviders(<Harness />);
    await shows("[NAME]\nSenior Backend Engineer\n[EMAIL]");
    const name = screen.getByTitle(en["cv.text.replaced"].replace("{{category}}", en["contact_category.name"]));
    expect(name.tagName).toBe("MARK");
    fireEvent.click(screen.getByRole("button", { name: en["cv.text.restore"].replace("{{category}}", en["contact_category.name"]) }));
    await settled();
    expect(puts()).toEqual([[{ op: "restore", replacement_index: 0 }]]);
    expect(puts()).toEqual([sentEdits("berlin_germany", 1)]);
    await shows("Berlin Germany\nSenior Backend Engineer\n[EMAIL]");
  });

  it('a header "Max Mustermann | Senior Backend Engineer" keeps the name until the user marks it', async () => {
    const api = fakeApi({ signedIn: true });
    const { puts } = review(api, "header_name_title");
    renderInProviders(<Harness />);
    await shows("Max Mustermann | Senior Backend Engineer\n[EMAIL]");
    expect(screen.getByText("Removed: 1 e-mail")).toBeTruthy();
    selectText("Max Mustermann");
    await mark();
    expect(puts()).toEqual([[{ op: "remove", start: 0, end: 14 }]]);
    await shows("[REMOVED] | Senior Backend Engineer\n[EMAIL]");
  });

  it("marks and restores a span after an emoji by code points", async () => {
    const api = fakeApi({ signedIn: true });
    const { puts } = review(api, "emoji");
    renderInProviders(<Harness />);
    await shows("🚀 Max Mustermann\nSenior Backend Engineer 😀\n[EMAIL]");
    selectText("Max Mustermann"); // UTF-16 [3, 17) after the rocket, code points [2, 16)
    await mark();
    expect(puts()).toEqual([[{ op: "remove", start: 2, end: 16 }]]);
    await shows("🚀 [REMOVED]\nSenior Backend Engineer 😀\n[EMAIL]");
    fireEvent.click(screen.getByRole("button", { name: en["cv.text.restore_removed"] }));
    await settled();
    expect(puts()).toEqual([sentEdits("emoji", 1), []]);
    await shows("🚀 Max Mustermann\nSenior Backend Engineer 😀\n[EMAIL]");
  });

  it("the selection maps to code points across text nodes", async () => {
    const api = fakeApi({ signedIn: true });
    const { puts } = review(api, "emoji");
    renderInProviders(<Harness />);
    await shows("🚀 Max Mustermann\nSenior Backend Engineer 😀\n[EMAIL]");
    // from "Engineer" in the second line, past its emoji and the line break, into [EMAIL] (a mark of its own)
    select({ needle: "Engineer" }, { needle: "[EMAIL]", at: 3 });
    await mark();
    // raw: "Engineer" starts at code point 32; the e-mail address ends the text at 58
    expect(puts()).toEqual([[{ op: "remove", start: 32, end: 58 }]]);
  });

  it("successive edits map through refreshed segments", async () => {
    const api = fakeApi({ signedIn: true });
    const { puts } = review(api, "successive");
    renderInProviders(<Harness />);
    await shows(reviewDraft("successive", 0).stripped_text_preview ?? "");
    selectText("0-170-1234567");
    await mark();
    await shows(reviewDraft("successive", 1).stripped_text_preview ?? "");
    expect(requestsTo(api, "GET /api/profile/drafts/7")).toHaveLength(2);
    selectText("01.02.1990");
    await mark();
    expect(puts()).toEqual([sentEdits("successive", 1), sentEdits("successive", 2)]);
    // the second set: the stored restore, the phone and the date of birth, each at its raw offsets
    expect(puts()[1]).toEqual([
      { op: "restore", start: 0, end: 14 },
      { op: "remove", start: 46, end: 59 },
      { op: "remove", start: 72, end: 82 },
    ]);
    await shows(reviewDraft("successive", 2).stripped_text_preview ?? "");
  });

  it("a refused edit says so and keeps the stored set", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/profile/drafts/7", () => json(reviewDraft("hyphen_phone")));
    api.routes.set("PUT /api/profile/drafts/7/text", () =>
      problem(422, "validation_failed", {
        errors: [{ field: "body.span_edits.0.start", code: "overlap", message: "Removed spans may not overlap each other." }],
      }),
    );
    renderInProviders(<Harness />);
    await shows(reviewDraft("hyphen_phone").stripped_text_preview ?? "");
    selectText("0-170-1234567");
    await mark();
    expect((await screen.findByRole("alert")).textContent).toBe(en["cv.text.refused"]);
    expect(requestsTo(api, "PUT /api/profile/drafts/7/text")).toHaveLength(1);
    expect(shown()).toBe(reviewDraft("hyphen_phone").stripped_text_preview);
  });

  it("a stored set that no longer applies is re-sent without the refused entries", async () => {
    const api = fakeApi({ signedIn: true });
    let reapplied = false;
    api.routes.set("GET /api/profile/drafts/7", () => json(reviewDraft("stale", reapplied ? 1 : 0)));
    api.routes.set("PUT /api/profile/drafts/7/text", (request) => {
      const { span_edits: edits } = JSON.parse(request.body ?? "{}") as { span_edits: SpanEditIn[] };
      if (edits.some((edit) => edit.op === "restore")) {
        return problem(422, "validation_failed", {
          errors: [{ field: "body.span_edits.0.start", code: "no_replacement", message: "The span names no replacement of the CV text." }],
        });
      }
      reapplied = true;
      return json({ stripped_text_preview: reviewDraft("stale", 1).stripped_text_preview });
    });
    renderInProviders(<Harness />);
    expect(await screen.findByText(en["cv.text.reapplying"])).toBeTruthy();
    await shows(reviewDraft("stale", 1).stripped_text_preview ?? "");
    const sent = requestsTo(api, "PUT /api/profile/drafts/7/text").map((r) => JSON.parse(r.body ?? "{}") as unknown);
    expect(sent).toEqual([
      { span_edits: [{ op: "restore", start: 39, end: 54 }, { op: "remove", start: 67, end: 77 }] },
      { span_edits: sentEdits("stale", 1) },
    ]);
    await settled(300);
    expect(requestsTo(api, "PUT /api/profile/drafts/7/text")).toHaveLength(2); // once per draft load, no loop
    expect(screen.queryByText(en["cv.text.reapplying"])).toBeNull();
  });

  it("a failed re-send offers Try again, and shows its problem", async () => {
    const api = fakeApi({ signedIn: true });
    let down = true;
    let reapplied = false;
    api.routes.set("GET /api/profile/drafts/7", () => json(reviewDraft("stale", reapplied ? 1 : 0)));
    api.routes.set("PUT /api/profile/drafts/7/text", (request) => {
      if (down) {
        return problem(503, "internal");
      }
      const { span_edits: edits } = JSON.parse(request.body ?? "{}") as { span_edits: SpanEditIn[] };
      if (edits.some((edit) => edit.op === "restore")) {
        return problem(422, "validation_failed", {
          errors: [{ field: "body.span_edits.0.start", code: "no_replacement", message: "The span names no replacement." }],
        });
      }
      reapplied = true;
      return json({ stripped_text_preview: reviewDraft("stale", 1).stripped_text_preview });
    });
    renderInProviders(<Harness />);
    const retry = await screen.findByRole("button", { name: en["cv.text.reapply_again"] });
    expect((await screen.findByRole("alert")).textContent).toContain(en["problem.internal"]);
    expect(requestsTo(api, "PUT /api/profile/drafts/7/text")).toHaveLength(1);
    expect(markButton().disabled).toBe(true); // still stale: no edit on a set that does not apply
    down = false;
    fireEvent.click(retry);
    await shows(reviewDraft("stale", 1).stripped_text_preview ?? "");
    expect(requestsTo(api, "PUT /api/profile/drafts/7/text")).toHaveLength(3); // the set, then without the refused entry
    expect(screen.queryByText(en["cv.text.reapplying"])).toBeNull();
  });

  it("a re-send refused for another reason shows its problem", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/profile/drafts/7", () => json(reviewDraft("stale", 0)));
    api.routes.set("PUT /api/profile/drafts/7/text", () =>
      problem(422, "validation_failed", {
        errors: [{ field: "body.span_edits.1.start", code: "overlap", message: "Removed spans may not overlap each other." }],
      }),
    );
    renderInProviders(<Harness />);
    expect((await screen.findByRole("alert")).textContent).toContain(en["problem.validation_failed"]);
    expect(screen.getByRole("button", { name: en["cv.text.reapply_again"] })).toBeTruthy();
    expect(requestsTo(api, "PUT /api/profile/drafts/7/text")).toHaveLength(1); // nothing named no_replacement
  });

  it("each mark names its category for assistive technology, outside the preview's text", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/profile/drafts/7", () => json(reviewDraft("hyphen_phone", 1)));
    renderInProviders(<Harness />);
    await shows("[NAME]\nSenior Backend Engineer\nMobil: [REMOVED]\nE-Mail: [EMAIL]");
    const marks = [...text().querySelectorAll("mark")].map((mark) => mark.textContent);
    expect(marks).toEqual([
      `${en["cv.text.mark_sr"].replace("{{category}}", en["contact_category.name"])} [NAME]`,
      `${en["cv.text.removed_sr"]} [REMOVED]`,
      `${en["cv.text.mark_sr"].replace("{{category}}", en["contact_category.email"])} [EMAIL]`,
    ]);
    expect(text().querySelector("mark .visually-hidden")?.closest("[data-preview]")).toBeNull();
  });

  it("Approve sends a keyed text-review", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/profile/drafts/7", () => json(reviewDraft("hyphen_phone")));
    api.routes.set("POST /api/profile/drafts/7/text-review", () => json({ status: "extracting_profile" }, 202));
    renderInProviders(<Harness />);
    await shows(reviewDraft("hyphen_phone").stripped_text_preview ?? "");
    expect(screen.getByText(en["cv.text.best_effort"])).toBeTruthy();
    const approve = screen.getByRole("button", { name: en["cv.text.approve"].replace("{{usd}}", "$0.14") });
    fireEvent.click(approve);
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/profile/drafts/7/text-review")).toHaveLength(1);
    });
    expect(requestsTo(api, "POST /api/profile/drafts/7/text-review")[0].headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/profile/drafts/7")).toHaveLength(2); // read again: it is extracting now
    });
  });

  it("a ready draft shows no approval", async () => {
    const api = fakeApi({ signedIn: true });
    const ready: DraftOut = reviewDraft("hyphen_phone", 0, { status: "ready" });
    api.routes.set("GET /api/profile/drafts/7", () => json(ready));
    renderInProviders(<Harness />);
    await shows(ready.stripped_text_preview ?? "");
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
    expect(screen.getByText(en["cv.text.reopens"])).toBeTruthy();
  });
});
