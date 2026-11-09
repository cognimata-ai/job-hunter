import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../../api/schema";
import en from "../../i18n/en.json";
import { type FakeApi, fakeApi, json, problem, profileList, requestsTo } from "../../test/api";
import { renderApp, settled } from "../../test/app";
import { draftOut, failedDraft, profile, readyDraft, reviewDraft } from "../../test/fixtures/drafts";

type DraftOut = components["schemas"]["DraftOut"];
type OpenDraftOut = components["schemas"]["OpenDraftOut"];
type SpanEditIn = components["schemas"]["SpanEditIn"];

const CV_TEXT = "Max Mustermann\nSenior Backend Engineer\n" + "Python, PostgreSQL, FastAPI. ".repeat(12);

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function withOpenDraft(api: FakeApi, open: OpenDraftOut | null) {
  api.routes.set("GET /api/profile", () => json({ ...profileList(), open_draft: open }));
}

function section(): HTMLElement {
  return screen.getByRole("region", { name: en["settings.section.cv"] });
}

function stepper(): HTMLElement {
  return within(section()).getByRole("list", { name: en["cv.status.label"] });
}

/** A step's name: its text without the hidden "done". */
function stepName(item: HTMLElement): string {
  return item.querySelector("span:not(.visually-hidden)")?.textContent ?? "";
}

function current(): string[] {
  return within(stepper())
    .getAllByRole("listitem")
    .filter((item) => item.getAttribute("aria-current") === "step")
    .map(stepName);
}

describe("CvSection", () => {
  it("without a draft: the upload and the active profile", async () => {
    const api = fakeApi({ signedIn: true });
    renderApp("/settings#cv");
    expect(await within(await screen.findByRole("region", { name: en["settings.section.cv"] })).findByText(
      "Active profile v3, confirmed 30 Oct 2026, 10:00",
    )).toBeTruthy();
    expect(within(section()).getByLabelText(en["cv.upload.browse"])).toBeTruthy();
    expect(within(section()).queryByRole("list", { name: en["cv.status.label"] })).toBeNull();
    expect(requestsTo(api, "GET /api/profile")).toHaveLength(1);
  });

  it("a reload finds the open draft", async () => {
    const api = fakeApi({ signedIn: true });
    withOpenDraft(api, { id: 7, status: "text_review" });
    api.routes.set("GET /api/profile/drafts/7", () => json(reviewDraft("hyphen_phone")));
    renderApp("/settings#cv");
    expect(await within(await screen.findByTestId("cv-text")).findByText(/Mobil: 0-170-1234567/)).toBeTruthy();
    expect(current()).toEqual([en["cv.step.review"]]);
    // the upload asks first now: a new CV discards this one
    expect(within(section()).getByLabelText(en["cv.upload.browse"])).toBeTruthy();
  });

  it("the stepper marks the current step", async () => {
    const api = fakeApi({ signedIn: true });
    let draft: DraftOut = draftOut({ status: "extracting_text" });
    withOpenDraft(api, { id: 7, status: "extracting_text" });
    api.routes.set("GET /api/profile/drafts/7", () => json(draft));
    const app = renderApp("/settings#cv");
    await waitFor(() => {
      expect(current()).toEqual([en["cv.step.extracting_text"], en["cv.step.removing_contacts"]]);
    });
    const items = within(stepper()).getAllByRole("listitem");
    expect(items.map(stepName)).toEqual([
      en["cv.step.uploading"],
      en["cv.step.extracting_text"],
      en["cv.step.removing_contacts"],
      en["cv.step.review"],
      en["cv.step.extracting_profile"].replace("{{model}}", "claude-opus-5-5"),
      en["cv.step.ready"],
    ]);
    expect(items[0].querySelector(".visually-hidden")?.textContent).toBe(en["cv.step.done"]);
    expect(within(section()).getByLabelText<HTMLInputElement>(en["cv.upload.browse"]).disabled).toBe(true);
    draft = draftOut({ status: "extracting_profile" });
    await act(() => app.queryClient.refetchQueries({ queryKey: ["profile", "draft", 7] }));
    await waitFor(() => {
      expect(current()).toEqual([en["cv.step.extracting_profile"].replace("{{model}}", "claude-opus-5-5")]);
    });
  });

  it("polls in the background while extracting and stops in review", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const api = fakeApi({ signedIn: true });
    let draft: DraftOut = draftOut({ status: "extracting_text" });
    withOpenDraft(api, { id: 7, status: "extracting_text" });
    api.routes.set("GET /api/profile/drafts/7", () => json(draft));
    renderApp("/settings#cv");
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/profile/drafts/7")).toHaveLength(1);
    });
    await settled();
    act(() => {
      vi.advanceTimersByTime(2_900);
    });
    expect(requestsTo(api, "GET /api/profile/drafts/7")).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(100);
    });
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/profile/drafts/7")).toHaveLength(2);
    });
    draft = reviewDraft("hyphen_phone");
    act(() => {
      vi.advanceTimersByTime(3_000);
    });
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/profile/drafts/7")).toHaveLength(3);
    });
    const [first, ...polls] = requestsTo(api, "GET /api/profile/drafts/7");
    expect(first.headers.has("X-JH-Background")).toBe(false); // the first read is the page's own
    expect(polls.map((r) => r.headers.get("X-JH-Background"))).toEqual(["1", "1"]);
    await within(section()).findByTestId("cv-text");
    act(() => {
      vi.advanceTimersByTime(9_000);
    });
    await settled();
    expect(requestsTo(api, "GET /api/profile/drafts/7")).toHaveLength(3); // the user's turn: no poll
  });

  it("shows Uploading while the paste is sent, then follows the new draft", async () => {
    const api = fakeApi({ signedIn: true });
    let answer: (response: Response) => void = () => undefined;
    api.routes.set("POST /api/profile/cv-text", () => new Promise<Response>((resolve) => { answer = resolve; }));
    api.routes.set("GET /api/profile/drafts/9", () => json(draftOut({ status: "uploaded" })));
    renderApp("/settings#cv");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: en["cv.paste.open"] }));
    fireEvent.change(screen.getByRole("textbox", { name: en["cv.paste.label"] }), { target: { value: CV_TEXT } });
    await user.click(screen.getByRole("button", { name: en["cv.paste.submit"] }));
    await waitFor(() => {
      expect(current()).toEqual([en["cv.step.uploading"]]);
    });
    answer(json({ draft_id: 9 }, 202));
    await waitFor(() => {
      expect(current()).toEqual([en["cv.step.extracting_text"], en["cv.step.removing_contacts"]]);
    });
    expect(requestsTo(api, "GET /api/profile/drafts/9")).toHaveLength(1);
  });

  it("a failed draft offers its repairs in order and a native retry needs the tick", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/profile/cv-text", () => json({ draft_id: 9 }, 202));
    let draft = failedDraft("invalid_output", ["paste_text", "native_pdf", "retry_llm"]);
    api.routes.set("GET /api/profile/drafts/9", () => json(draft));
    api.routes.set("POST /api/profile/drafts/9/retry", () => {
      draft = draftOut({ status: "extracting_profile" });
      return json({ status: "extracting_profile" }, 202);
    });
    renderApp("/settings#cv");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: en["cv.paste.open"] }));
    fireEvent.change(screen.getByRole("textbox", { name: en["cv.paste.label"] }), { target: { value: CV_TEXT } });
    await user.click(screen.getByRole("button", { name: en["cv.paste.submit"] }));
    const failure = await within(section()).findByRole("group", { name: en["cv.failure.title"] });
    expect(within(failure).getByText(en["draft_failure.invalid_output"])).toBeTruthy();
    expect(within(failure).getAllByRole("button").map((b) => b.textContent)).toEqual([
      en["draft_repair.paste_text"],
      en["draft_repair.native_pdf"],
      en["draft_repair.retry_llm"],
      en["cv.failure.start_again"],
    ]);
    const native = within(failure).getByRole("button", { name: en["draft_repair.native_pdf"] });
    expect(native.getAttribute("aria-expanded")).toBe("false");
    await user.click(native);
    expect(native.getAttribute("aria-expanded")).toBe("true");
    await user.click(native); // a second press closes it again
    expect(native.getAttribute("aria-expanded")).toBe("false");
    expect(within(failure).queryByRole("button", { name: en["cv.failure.native_send"] })).toBeNull();
    await user.click(native);
    const send = within(failure).getByRole<HTMLButtonElement>("button", { name: en["cv.failure.native_send"] });
    expect(within(failure).getByText(/will be sent to Anthropic\. Contact stripping cannot be applied\.$/)).toBeTruthy();
    expect(send.disabled).toBe(true);
    fireEvent.click(send);
    expect(requestsTo(api, "POST /api/profile/drafts/9/retry")).toHaveLength(0);
    await user.click(within(failure).getByRole("checkbox", { name: en["cv.failure.native_ack"] }));
    await user.click(send);
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/profile/drafts/9/retry")).toHaveLength(1);
    });
    const [retry] = requestsTo(api, "POST /api/profile/drafts/9/retry");
    expect(JSON.parse(retry.body ?? "null")).toEqual({ mode: "native_pdf", ack_native_pdf: true });
    expect(retry.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    // 202: polled again, now extracting the profile
    await waitFor(() => {
      expect(current()).toEqual([en["cv.step.extracting_profile"].replace("{{model}}", "claude-opus-5-5")]);
    });
  });

  it("Extract again sends the llm mode; an unknown code shows the generic text with its code", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/profile/cv-text", () => json({ draft_id: 9 }, 202));
    api.routes.set("GET /api/profile/drafts/9", () => json(failedDraft("llm_request_529", ["retry_llm"])));
    api.routes.set("POST /api/profile/drafts/9/retry", () => json({ status: "extracting_profile" }, 202));
    renderApp("/settings#cv");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: en["cv.paste.open"] }));
    fireEvent.change(screen.getByRole("textbox", { name: en["cv.paste.label"] }), { target: { value: CV_TEXT } });
    await user.click(screen.getByRole("button", { name: en["cv.paste.submit"] }));
    const failure = await within(section()).findByRole("group", { name: en["cv.failure.title"] });
    expect(within(failure).getByText(en["cv.failure.unknown"])).toBeTruthy();
    expect(within(failure).getByText("llm_request_529").tagName).toBe("CODE");
    await user.click(within(failure).getByRole("button", { name: en["draft_repair.retry_llm"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/profile/drafts/9/retry")).toHaveLength(1);
    });
    expect(JSON.parse(requestsTo(api, "POST /api/profile/drafts/9/retry")[0].body ?? "null")).toEqual({ mode: "llm" });
  });

  it("the paste repair opens the paste form; Upload another CV starts again", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/profile/cv-text", () => json({ draft_id: 9 }, 202));
    api.routes.set("GET /api/profile/drafts/9", () => json(failedDraft("low_text", ["paste_text"])));
    renderApp("/settings#cv");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: en["cv.paste.open"] }));
    fireEvent.change(screen.getByRole("textbox", { name: en["cv.paste.label"] }), { target: { value: CV_TEXT } });
    await user.click(screen.getByRole("button", { name: en["cv.paste.submit"] }));
    const failure = await within(section()).findByRole("group", { name: en["cv.failure.title"] });
    expect(within(section()).queryByRole("textbox", { name: en["cv.paste.label"] })).toBeNull();
    await user.click(within(failure).getByRole("button", { name: en["draft_repair.paste_text"] }));
    expect(within(section()).getByRole("textbox", { name: en["cv.paste.label"] })).toBe(document.activeElement);
    // closed by its own toggle (the disclosure button beside the upload), the repair opens it again
    const toggle = within(section())
      .getAllByRole("button", { name: en["cv.paste.open"] })
      .find((button) => button.hasAttribute("aria-expanded"));
    await user.click(toggle ?? section());
    expect(within(section()).queryByRole("textbox", { name: en["cv.paste.label"] })).toBeNull();
    await user.click(within(failure).getByRole("button", { name: en["draft_repair.paste_text"] }));
    expect(within(section()).getByRole("textbox", { name: en["cv.paste.label"] })).toBeTruthy();
    await user.click(within(failure).getByRole("button", { name: en["cv.failure.start_again"] }));
    expect(within(section()).queryByRole("group", { name: en["cv.failure.title"] })).toBeNull();
    expect(within(section()).getByText("Active profile v3, confirmed 30 Oct 2026, 10:00")).toBeTruthy();
  });

  it("a draft read as confirmed or gone leaves the review and reads the profile again", async () => {
    const api = fakeApi({ signedIn: true });
    let open: OpenDraftOut | null = { id: 7, status: "ready" };
    api.routes.set("GET /api/profile", () => json({ ...profileList(), open_draft: open }));
    let draft: DraftOut = readyDraft();
    api.routes.set("GET /api/profile/drafts/7", () => json(draft));
    const app = renderApp("/settings#cv");
    const cv = await screen.findByRole("region", { name: en["settings.section.cv"] });
    expect(await within(cv).findByRole("form", { name: en["cv.profile.title"] })).toBeTruthy();
    draft = { ...draft, status: "confirmed" };
    open = null;
    await act(() => app.queryClient.refetchQueries({ queryKey: ["profile", "draft", 7] }));
    await waitFor(() => {
      expect(within(section()).queryByRole("form", { name: en["cv.profile.title"] })).toBeNull();
    });
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/profile")).toHaveLength(2);
    });
    expect(await within(section()).findByText("Active profile v3, confirmed 30 Oct 2026, 10:00")).toBeTruthy();
  });

  it("a draft found through open_draft keeps its repairs once it has failed", async () => {
    const api = fakeApi({ signedIn: true });
    let open: OpenDraftOut | null = { id: 7, status: "extracting_profile" };
    let draft: DraftOut = draftOut({ status: "extracting_profile" });
    api.routes.set("GET /api/profile", () => json({ ...profileList(), open_draft: open }));
    api.routes.set("GET /api/profile/drafts/7", () => json(draft));
    const app = renderApp("/settings#cv");
    await waitFor(() => {
      expect(current()).toEqual([en["cv.step.extracting_profile"].replace("{{model}}", "claude-opus-5-5")]);
    });
    // failed: no longer open, so the next profile read names no draft
    draft = failedDraft("invalid_output", ["retry_llm"]);
    open = null;
    await act(() => app.queryClient.refetchQueries({ queryKey: ["profile"] }));
    const failure = await within(section()).findByRole("group", { name: en["cv.failure.title"] });
    expect(within(failure).getByRole("button", { name: en["draft_repair.retry_llm"] })).toBeTruthy();
  });

  it("a stale set in ready is not re-sent on load", async () => {
    const api = fakeApi({ signedIn: true });
    withOpenDraft(api, { id: 7, status: "ready" });
    api.routes.set("GET /api/profile/drafts/7", () => json(reviewDraft("stale", 0, { status: "ready", draft: profile() })));
    renderApp("/settings#cv");
    const note = await within(await screen.findByRole("region", { name: en["cv.profile.title"] })).findByText(en["cv.text.reapplying_ready"]);
    expect(note.closest("details")).toBeNull(); // beside the strip report, not inside the collapsed text
    await settled(300);
    expect(requestsTo(api, "PUT /api/profile/drafts/7/text")).toHaveLength(0);
    expect(within(section()).getByRole<HTMLButtonElement>("button", { name: en["cv.text.mark"] }).disabled).toBe(true);
  });

  it("re-applying a stale set in ready asks first", async () => {
    const api = fakeApi({ signedIn: true });
    let reapplied = false;
    withOpenDraft(api, { id: 7, status: "ready" });
    api.routes.set("GET /api/profile/drafts/7", () =>
      json(reapplied ? reviewDraft("stale", 1) : reviewDraft("stale", 0, { status: "ready", draft: profile() })),
    );
    api.routes.set("PUT /api/profile/drafts/7/text", (request) => {
      const { span_edits: edits } = JSON.parse(request.body ?? "{}") as { span_edits: SpanEditIn[] };
      if (edits.some((edit) => edit.op === "restore")) {
        return problem(422, "validation_failed", {
          errors: [{ field: "body.span_edits.0.start", code: "no_replacement", message: "The span names no replacement." }],
        });
      }
      reapplied = true;
      return json({ stripped_text_preview: reviewDraft("stale", 1).stripped_text_preview });
    });
    renderApp("/settings#cv");
    const user = userEvent.setup();
    const reapply = await screen.findByRole("button", { name: en["cv.text.reapply"] });
    await user.click(reapply);
    const dialog = screen.getByRole("alertdialog", { name: en["cv.reapply.title"] });
    expect(within(dialog).getByText(en["cv.reapply.body"])).toBeTruthy();
    await user.click(within(dialog).getByRole("button", { name: en["action.cancel"] }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(requestsTo(api, "PUT /api/profile/drafts/7/text")).toHaveLength(0);
    await user.click(reapply);
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: en["cv.reapply.confirm"] }));
    // the same once-only re-send: the stored set, then without the refused restore; the text review reopens
    expect(await within(section()).findByRole("region", { name: en["cv.text.title"] })).toBeTruthy();
    const puts = requestsTo(api, "PUT /api/profile/drafts/7/text");
    expect(puts.map((r) => (JSON.parse(r.body ?? "{}") as { span_edits: SpanEditIn[] }).span_edits)).toEqual([
      [{ op: "restore", start: 39, end: 54 }, { op: "remove", start: 67, end: 77 }],
      [{ op: "remove", start: 67, end: 77 }],
    ]);
    // `PUT …/text` is not a keyed route (§14.1; the route takes no Idempotency-Key)
    expect(puts.every((r) => !r.headers.has("Idempotency-Key"))).toBe(true);
  });
});
