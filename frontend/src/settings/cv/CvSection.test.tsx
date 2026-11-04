import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../../api/schema";
import en from "../../i18n/en.json";
import { type FakeApi, fakeApi, json, profileList, requestsTo } from "../../test/api";
import { renderApp, settled } from "../../test/app";
import { draftOut, failedDraft, readyDraft, reviewDraft } from "../../test/fixtures/drafts";

type DraftOut = components["schemas"]["DraftOut"];
type OpenDraftOut = components["schemas"]["OpenDraftOut"];

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
    await user.click(within(failure).getByRole("button", { name: en["draft_repair.native_pdf"] }));
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
});
