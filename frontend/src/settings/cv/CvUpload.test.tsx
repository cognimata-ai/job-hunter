import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../../i18n/en.json";
import { fakeApi, json, problem, requestsTo } from "../../test/api";
import { renderInProviders } from "../../test/app";
import { CvUpload } from "./CvUpload";

const DISCLOSURE =
  "The original file, including your name, contact details and photo, will be sent to Anthropic. Contact " +
  "stripping cannot be applied.";
const PDF = () => new File(["%PDF-1.7 synthetic"], "max-mustermann.pdf", { type: "application/pdf" });
const DOCX = () =>
  new File(["PK synthetic"], "max-mustermann.docx", {
    type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  });
const CV_TEXT = "Max Mustermann\nSenior Backend Engineer\n" + "Python, PostgreSQL, FastAPI. ".repeat(12);

afterEach(() => {
  vi.restoreAllMocks();
});

function fileInput(): HTMLInputElement {
  return screen.getByLabelText<HTMLInputElement>(en["cv.upload.browse"]);
}

function nativeBox(): HTMLInputElement {
  return screen.getByRole<HTMLInputElement>("checkbox", { name: en["cv.upload.native"] });
}

function uploadButton(): HTMLButtonElement {
  return screen.getByRole<HTMLButtonElement>("button", { name: en["cv.upload.submit"] });
}

function describedBy(element: HTMLElement): string {
  return (element.getAttribute("aria-describedby") ?? "")
    .split(" ")
    .map((id) => document.getElementById(id)?.textContent ?? "")
    .join(" ");
}

describe("CvUpload", () => {
  it("the disclosure text", async () => {
    fakeApi({ signedIn: true });
    renderInProviders(<CvUpload openStatus={null} onCreated={vi.fn()} />);
    const input = fileInput();
    expect(input.type).toBe("file");
    expect(input.accept).toBe(
      ".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    );
    // the disclosure is the checkbox's description, in the tree whether or not ⓘ shows it
    expect(describedBy(nativeBox())).toBe(DISCLOSURE);
    expect(nativeBox().disabled).toBe(true); // no PDF picked
    const info = screen.getByRole("button", { name: en["cv.upload.native_info"] });
    expect(info.getAttribute("aria-expanded")).toBe("false");
    const user = userEvent.setup();
    await user.click(info);
    expect(info.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText(DISCLOSURE).className).not.toContain("visually-hidden");
    await user.upload(input, DOCX());
    expect(nativeBox().disabled).toBe(true); // a DOCX has no original PDF to send
    await user.upload(input, PDF());
    expect(nativeBox().disabled).toBe(false);
  });

  it("uploads the file, and the native opt-in when ticked, with no key", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/profile/cv", () => json({ draft_id: 42 }, 202));
    const onCreated = vi.fn();
    renderInProviders(<CvUpload openStatus={null} onCreated={onCreated} />);
    const user = userEvent.setup();
    expect(uploadButton().disabled).toBe(true);
    const file = PDF();
    await user.upload(fileInput(), file);
    expect(screen.getByText("max-mustermann.pdf")).toBeTruthy();
    await user.click(nativeBox());
    await user.click(uploadButton());
    await waitFor(() => {
      expect(onCreated).toHaveBeenCalledWith(42);
    });
    const [request] = requestsTo(api, "POST /api/profile/cv");
    expect(request.form?.get("file")).toBe(file);
    expect(request.form?.get("native_pdf_opt_in")).toBe("true");
    expect(request.headers.has("Idempotency-Key")).toBe(false);
    expect(request.headers.get("X-JH-Request")).toBe("1");
  });

  it("a drop of one file picks it, of several is refused with a text", () => {
    fakeApi({ signedIn: true });
    renderInProviders(<CvUpload openStatus={null} onCreated={vi.fn()} />);
    const zone = screen.getByTestId("cv-drop");
    fireEvent.drop(zone, { dataTransfer: { files: [PDF(), DOCX()] } });
    expect(screen.getByText(en["cv.upload.drop_one"])).toBeTruthy();
    expect(uploadButton().disabled).toBe(true);
    fireEvent.drop(zone, { dataTransfer: { files: [PDF()] } });
    expect(screen.queryByText(en["cv.upload.drop_one"])).toBeNull();
    expect(screen.getByText("max-mustermann.pdf")).toBeTruthy();
    expect(uploadButton().disabled).toBe(false);
  });

  it("paste text path", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/profile/cv-text", () => json({ draft_id: 43 }, 202));
    const onCreated = vi.fn();
    renderInProviders(<CvUpload openStatus={null} onCreated={onCreated} />);
    const toggle = screen.getByRole("button", { name: en["cv.paste.open"] });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    const user = userEvent.setup();
    await user.click(toggle);
    const area = screen.getByRole<HTMLTextAreaElement>("textbox", { name: en["cv.paste.label"] });
    fireEvent.change(area, { target: { value: CV_TEXT } });
    expect(describedBy(area)).toContain(`${String(new TextEncoder().encode(CV_TEXT).length)} / 1,048,576`);
    await user.click(screen.getByRole("button", { name: en["cv.paste.submit"] }));
    await waitFor(() => {
      expect(onCreated).toHaveBeenCalledWith(43);
    });
    const [request] = requestsTo(api, "POST /api/profile/cv-text");
    expect(JSON.parse(request.body ?? "null")).toEqual({ text: CV_TEXT });
    expect(request.headers.has("Idempotency-Key")).toBe(false);
  });

  it("the paste refuses over 1 MiB before sending, and an empty text", async () => {
    const api = fakeApi({ signedIn: true });
    renderInProviders(<CvUpload openStatus={null} onCreated={vi.fn()} pasteOpen />);
    const area = screen.getByRole<HTMLTextAreaElement>("textbox", { name: en["cv.paste.label"] });
    const submit = screen.getByRole("button", { name: en["cv.paste.submit"] });
    fireEvent.click(submit);
    expect((await screen.findByRole("alert")).textContent).toBe(en["cv.paste.empty"]);
    // 2^20 − 1 bytes of "ä" (2 bytes each) and one more "ä": over the cap by one byte
    fireEvent.change(area, { target: { value: "a" + "ä".repeat(2 ** 19) } });
    expect(area.getAttribute("aria-invalid")).toBe("true");
    fireEvent.click(submit);
    expect((await screen.findByRole("alert")).textContent).toBe(en["cv.paste.too_large"].replace("{{max}}", "1,048,576"));
    expect(requestsTo(api, "POST /api/profile/cv-text")).toHaveLength(0);
  });

  it("asks before replacing an open draft", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/profile/cv", () => json({ draft_id: 44 }, 202));
    const onCreated = vi.fn();
    renderInProviders(<CvUpload openStatus="text_review" onCreated={onCreated} />);
    const user = userEvent.setup();
    await user.upload(fileInput(), DOCX());
    await user.click(uploadButton());
    const dialog = screen.getByRole("alertdialog", { name: en["cv.replace.title"] });
    expect(within(dialog).getByText(en["cv.replace.body"])).toBeTruthy();
    expect(requestsTo(api, "POST /api/profile/cv")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: en["action.cancel"] }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(requestsTo(api, "POST /api/profile/cv")).toHaveLength(0);
    await user.click(uploadButton());
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: en["cv.replace.confirm"] }));
    await waitFor(() => {
      expect(onCreated).toHaveBeenCalledWith(44);
    });
    expect(requestsTo(api, "POST /api/profile/cv")).toHaveLength(1);
  });

  it("the paste over a draft in review asks too", () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/profile/cv-text", () => json({ draft_id: 45 }, 202));
    renderInProviders(<CvUpload openStatus="ready" onCreated={vi.fn()} pasteOpen />);
    fireEvent.change(screen.getByRole("textbox", { name: en["cv.paste.label"] }), { target: { value: CV_TEXT } });
    fireEvent.click(screen.getByRole("button", { name: en["cv.paste.submit"] }));
    expect(screen.getByRole("alertdialog", { name: en["cv.replace.title"] })).toBeTruthy();
    expect(requestsTo(api, "POST /api/profile/cv-text")).toHaveLength(0);
  });

  it("while a CV is being processed both are disabled, with the text", () => {
    fakeApi({ signedIn: true });
    renderInProviders(<CvUpload openStatus="extracting_profile" onCreated={vi.fn()} />);
    expect(fileInput().disabled).toBe(true);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: en["cv.paste.open"] }).disabled).toBe(true);
    expect(screen.getByText(en["cv.upload.processing"])).toBeTruthy();
  });

  it("upload_rejected shows the reason", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/profile/cv", () =>
      problem(422, "upload_rejected", { errors: [{ field: "file", code: "docx_macro", message: "macros" }] }),
    );
    renderInProviders(<CvUpload openStatus={null} onCreated={vi.fn()} />);
    const user = userEvent.setup();
    await user.upload(fileInput(), DOCX());
    await user.click(uploadButton());
    expect((await screen.findByRole("alert")).textContent).toContain(en["upload_reason.docx_macro"]);
  });

  it("a 413 of the paste shows its reason too", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/profile/cv-text", () =>
      problem(413, "upload_rejected", { errors: [{ field: "text", code: "too_large", message: "too large" }] }),
    );
    renderInProviders(<CvUpload openStatus={null} onCreated={vi.fn()} pasteOpen />);
    fireEvent.change(screen.getByRole("textbox", { name: en["cv.paste.label"] }), { target: { value: CV_TEXT } });
    fireEvent.click(screen.getByRole("button", { name: en["cv.paste.submit"] }));
    expect((await screen.findByRole("alert")).textContent).toContain(en["upload_reason.too_large"]);
  });

  it("a refused opt-in shows at the checkbox; a 409 says a CV is being processed", async () => {
    const api = fakeApi({ signedIn: true });
    const answers = [
      () =>
        problem(422, "validation_failed", {
          errors: [{ field: "native_pdf_opt_in", code: "native_pdf_disabled", message: "Sending the original PDF is not enabled" }],
        }),
      () => problem(409, "conflict_state"),
    ];
    api.routes.set("POST /api/profile/cv", () => (answers.shift() ?? answers[0])());
    renderInProviders(<CvUpload openStatus={null} onCreated={vi.fn()} />);
    const user = userEvent.setup();
    await user.upload(fileInput(), PDF());
    await user.click(nativeBox());
    await user.click(uploadButton());
    await waitFor(() => {
      expect(nativeBox().getAttribute("aria-invalid")).toBe("true");
    });
    expect(describedBy(nativeBox())).toContain("Sending the original PDF is not enabled");
    await user.click(uploadButton());
    expect((await screen.findByRole("alert")).textContent).toBe(en["cv.upload.conflict"]);
  });
});
