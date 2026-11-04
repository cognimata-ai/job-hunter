import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import { fakeApi, json, problem, requestsTo } from "../test/api";
import { NOW, renderInProviders } from "../test/app";
import { FeedbackTab } from "./FeedbackTab";

type JobFeedbackOut = components["schemas"]["JobFeedbackOut"];

const FEEDBACK: JobFeedbackOut[] = [
  { id: 78, kind: "wrong_score", label: "poor", comment: "<b>Too high</b>", created_at: "2026-11-03T06:00:00Z" },
  { id: 77, kind: "label", label: "good", comment: null, created_at: "2026-11-02T06:00:00Z" },
];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function form(): HTMLElement {
  return screen.getByRole("form", { name: en["feedback.title"] });
}

function comment(): HTMLTextAreaElement {
  return within(form()).getByRole<HTMLTextAreaElement>("textbox", { name: en["feedback.comment"] });
}

describe("FeedbackTab", () => {
  it("a label posts a wrong_score with its comment, and invalidates the detail", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/jobs/5/feedback", () => json({ feedback_id: 80, golden_case_id: 3 }, 201));
    const app = renderInProviders(<FeedbackTab jobId={5} feedback={[]} />);
    const invalidate = vi.spyOn(app.queryClient, "invalidateQueries");
    const user = userEvent.setup();
    const labels = within(form()).getByRole("radiogroup", { name: en["feedback.label"] });
    expect(within(labels).getAllByRole("radio").map((radio) => radio.getAttribute("value"))).toEqual(["good", "ok", "poor"]);
    await user.click(within(labels).getByRole("radio", { name: en["feedback_label.poor"] }));
    await user.type(comment(), "Too high for me");
    await user.click(within(form()).getByRole("button", { name: en["feedback.submit"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/jobs/5/feedback")).toHaveLength(1);
    });
    const [request] = requestsTo(api, "POST /api/jobs/5/feedback");
    expect(JSON.parse(request.body ?? "null")).toEqual({ kind: "wrong_score", label: "poor", comment: "Too high for me" });
    expect(request.headers.has("Idempotency-Key")).toBe(false);
    expect(await screen.findByText(en["feedback.sent"])).toBeTruthy();
    expect(comment().value).toBe(""); // the form starts again
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["jobs", "detail", 5] });
  });

  it("a label without a comment posts none", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/jobs/5/feedback", () => json({ feedback_id: 80, golden_case_id: 3 }, 201));
    renderInProviders(<FeedbackTab jobId={5} feedback={[]} />);
    const user = userEvent.setup();
    const submit = within(form()).getByRole("button", { name: en["feedback.submit"] });
    expect((submit as HTMLButtonElement).disabled).toBe(true); // no label chosen yet
    await user.click(within(form()).getByRole("radio", { name: en["feedback_label.good"] }));
    await user.type(comment(), "   ");
    await user.click(submit);
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/jobs/5/feedback")).toHaveLength(1);
    });
    expect(JSON.parse(requestsTo(api, "POST /api/jobs/5/feedback")[0].body ?? "null")).toEqual({ kind: "wrong_score", label: "good" });
  });

  it("409 shows the message", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/jobs/5/feedback", () => problem(409, "conflict_state"));
    renderInProviders(<FeedbackTab jobId={5} feedback={[]} />);
    const user = userEvent.setup();
    await user.click(within(form()).getByRole("radio", { name: en["feedback_label.ok"] }));
    await user.click(within(form()).getByRole("button", { name: en["feedback.submit"] }));
    const alert = await within(form()).findByRole("alert");
    expect(alert.textContent).toBe(en["feedback.no_score"]);
    expect(screen.queryByText(en["feedback.sent"])).toBeNull();
  });

  it("the comment counts code points", () => {
    fakeApi({ signedIn: true });
    renderInProviders(<FeedbackTab jobId={5} feedback={[]} />);
    const count = () => within(form()).getByText(/\/ 2,000$/).textContent;
    fireEvent.change(comment(), { target: { value: "👋🏽ab" } }); // 4 code points, 6 UTF-16 units
    expect(count()).toBe("4 / 2,000");
    fireEvent.click(within(form()).getByRole("radio", { name: en["feedback_label.good"] }));
    const submit = within(form()).getByRole<HTMLButtonElement>("button", { name: en["feedback.submit"] });
    fireEvent.change(comment(), { target: { value: "😀".repeat(2000) } }); // 4,000 UTF-16 units: still allowed
    expect(count()).toBe("2,000 / 2,000");
    expect(submit.disabled).toBe(false);
    expect(comment().getAttribute("aria-invalid")).toBe("false");
    fireEvent.change(comment(), { target: { value: "😀".repeat(2001) } });
    expect(count()).toBe("2,001 / 2,000");
    expect(submit.disabled).toBe(true);
    expect(comment().getAttribute("aria-invalid")).toBe("true");
    const describedBy = comment().getAttribute("aria-describedby") ?? "";
    const hint = describedBy.split(" ").map((id) => document.getElementById(id)?.textContent);
    expect(hint).toContain(en["feedback.too_long"].replace("{{max}}", "2,000"));
  });

  it("lists the job's feedback newest first, its comment as text, and Delete calls DELETE /api/feedback/{id}", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("DELETE /api/feedback/77", () => new Response(null, { status: 204 }));
    const app = renderInProviders(<FeedbackTab jobId={5} feedback={FEEDBACK} />);
    const invalidate = vi.spyOn(app.queryClient, "invalidateQueries");
    const list = screen.getByRole("list", { name: en["feedback.list"] });
    const [newest, older] = within(list).getAllByRole("listitem");
    expect(newest.textContent).toContain(en["feedback_label.poor"]);
    expect(newest.textContent).toContain("<b>Too high</b>");
    expect(newest.querySelector("b")).toBeNull();
    expect(older.textContent).toContain(en["feedback_label.good"]);
    const user = userEvent.setup();
    await user.click(within(older).getByRole("button", { name: en["feedback.delete"] }));
    expect(screen.queryByRole("alertdialog")).toBeNull(); // no confirmation
    await waitFor(() => {
      expect(requestsTo(api, "DELETE /api/feedback/77")).toHaveLength(1);
    });
    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["jobs", "detail", 5] });
    });
  });

  it("without feedback says so", () => {
    fakeApi({ signedIn: true });
    renderInProviders(<FeedbackTab jobId={5} feedback={[]} />);
    expect(screen.getByText(en["feedback.none"])).toBeTruthy();
  });
});
