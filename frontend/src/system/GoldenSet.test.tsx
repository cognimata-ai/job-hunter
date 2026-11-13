import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { fakeApi, goldenCase, json, manifestOut, problem, requestsTo } from "../test/api";
import tables from "../settings/tables.module.css";
import { renderInProviders } from "../test/app";
import { GoldenSet } from "./GoldenSet";

afterEach(() => {
  vi.restoreAllMocks();
});

function rowOf(title: string): HTMLElement {
  const row = screen.getByText(title, { selector: "td" }).closest("tr");
  if (row === null) {
    throw new Error(`no row for ${title}`);
  }
  return row;
}

describe("GoldenSet", () => {
  it("the title spans its card's row in the narrow layout, so a long title never runs into the company", async () => {
    // Task 36's visual pass: `tables.text` keeps a cell at least 10rem wide, more than a card column (8.5rem), so a
    // title alone in its column ran into the company beside it; the queries' and watchlist's text cells span the row
    fakeApi({ signedIn: true }).goldenCases = [goldenCase({ title: "Partnerships Manager" })];
    renderInProviders(<GoldenSet />);
    const cell = await screen.findByText("Partnerships Manager", { selector: "td" });
    expect(tables.wide).toBeTruthy();
    expect(cell.classList.contains(tables.text)).toBe(true);
    expect(cell.classList.contains(tables.wide)).toBe(true);
  });

  it("titles, toggles and delete with confirm", async () => {
    const api = fakeApi({ signedIn: true });
    api.goldenCases = [
      goldenCase(),
      goldenCase({ id: 2, job_id: 314, title: "Softwareentwickler Java", company: "Testdaten Systems GmbH", label: "ok", source: "adversarial", is_canary: true }),
    ];
    let answer: (response: Response) => void = () => undefined;
    api.routes.set("PATCH /api/golden-cases/1", () => new Promise<Response>((resolve) => { answer = resolve; }));
    api.routes.set("DELETE /api/golden-cases/2", () => {
      api.goldenCases = [goldenCase({ active: false })];
      return json({ manifest_id: 9 }, 202);
    });
    api.manifests = [manifestOut({ id: 9, kind: "golden_case_delete", status: "executing", completed_at: null, shipped_at: null })];
    renderInProviders(<GoldenSet />);
    const table = await screen.findByRole("table", { name: en["settings_system.golden"] });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => [...row.querySelectorAll("td")].slice(0, 4).map((cell) => cell.textContent))).toEqual([
      ["Backend Engineer (Python)", "Beispiel GmbH", en["feedback_label.good"], en["golden_source.week1"]],
      ["Softwareentwickler Java", "Testdaten Systems GmbH", en["feedback_label.ok"], en["golden_source.adversarial"]],
    ]);

    // a toggle sends its flag alone, and is disabled while it runs
    const active = within(rowOf("Backend Engineer (Python)")).getByRole<HTMLInputElement>("checkbox", { name: "Active: Backend Engineer (Python)" });
    expect(active.checked).toBe(true);
    fireEvent.click(active);
    await waitFor(() => {
      expect(requestsTo(api, "PATCH /api/golden-cases/1")).toHaveLength(1);
    });
    expect(JSON.parse(requestsTo(api, "PATCH /api/golden-cases/1")[0]?.body ?? "")).toEqual({ active: false });
    expect(requestsTo(api, "PATCH /api/golden-cases/1")[0]?.headers.get("Idempotency-Key")).toBeNull();
    expect(active.disabled).toBe(true);
    api.goldenCases = [goldenCase({ active: false }), api.goldenCases[1]];
    answer(json(goldenCase({ active: false })));
    await waitFor(() => {
      expect(active.disabled).toBe(false);
    });
    await waitFor(() => {
      expect(active.checked).toBe(false);
    });

    // a refused toggle (a third canary) shows at its row
    api.routes.set("PATCH /api/golden-cases/1", () => problem(422, "validation_failed", { title: "At most two canaries" }));
    fireEvent.click(within(rowOf("Backend Engineer (Python)")).getByRole("checkbox", { name: "Canary: Backend Engineer (Python)" }));
    await waitFor(() => {
      expect(JSON.parse(requestsTo(api, "PATCH /api/golden-cases/1").at(-1)?.body ?? "")).toEqual({ is_canary: true });
    });
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain(en["problem.validation_failed"]);

    // Delete asks first, naming the case; Cancel sends nothing
    fireEvent.click(within(rowOf("Softwareentwickler Java")).getByRole("button", { name: /^Delete/ }));
    const dialog = screen.getByRole("alertdialog", { name: en["golden.delete_title"] });
    expect(dialog.textContent).toContain("Softwareentwickler Java");
    fireEvent.click(within(dialog).getByRole("button", { name: en["action.cancel"] }));
    expect(requestsTo(api, "DELETE /api/golden-cases/2")).toHaveLength(0);
    fireEvent.click(within(rowOf("Softwareentwickler Java")).getByRole("button", { name: /^Delete/ }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: en["golden.delete"] }));
    await waitFor(() => {
      expect(requestsTo(api, "DELETE /api/golden-cases/2")).toHaveLength(1);
    });
    expect(requestsTo(api, "DELETE /api/golden-cases/2")[0]?.headers.get("Idempotency-Key")).toBeNull();
    await waitFor(() => {
      expect(screen.queryByText("Softwareentwickler Java", { selector: "td" })).toBeNull();
    });
    const requested = within(await screen.findByRole("region", { name: en["golden.deletions"] })).getByText("Softwareentwickler Java");
    expect(requested.closest("li")?.textContent).toContain(en["golden.deletion_requested"]);
  });

  it("a delete shows the deletion status", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const api = fakeApi({ signedIn: true });
      api.goldenCases = [goldenCase()];
      let answer: (response: Response) => void = () => undefined;
      api.routes.set("DELETE /api/golden-cases/1", () => new Promise<Response>((resolve) => { answer = resolve; }));
      api.manifests = [manifestOut({ id: 9, kind: "golden_case_delete", status: "executing", completed_at: null, shipped_at: null })];
      renderInProviders(<GoldenSet />);
      await screen.findByText("Backend Engineer (Python)", { selector: "td" });
      const remove = within(rowOf("Backend Engineer (Python)")).getByRole<HTMLButtonElement>("button", { name: /^Delete/ });
      fireEvent.click(remove);
      const confirm = within(screen.getByRole("alertdialog")).getByRole<HTMLButtonElement>("button", { name: en["golden.delete"] });
      // a double confirm sends one delete; the confirm and the row's Delete are busy until the answer
      fireEvent.click(confirm);
      fireEvent.click(confirm);
      await waitFor(() => {
        expect(confirm.disabled).toBe(true);
      });
      expect(remove.disabled).toBe(true);
      expect(requestsTo(api, "DELETE /api/golden-cases/1")).toHaveLength(1);
      answer(json({ manifest_id: 9 }, 202));
      await waitFor(() => {
        expect(screen.queryByRole("alertdialog")).toBeNull();
      });

      // "Deletion requested", naming the case, with the manifest's status, read every 10 s until confirmed offsite
      const status = await screen.findByRole("region", { name: en["golden.deletions"] });
      const requested = within(status).getByText(/Backend Engineer \(Python\)/).closest("li");
      expect(requested?.textContent).toContain(en["golden.deletion_requested"]);
      await waitFor(() => {
        expect(requested?.textContent).toContain(en["settings_privacy.status.executing"]);
      });
      const reads = () => requestsTo(api, "GET /api/privacy/manifests/9");
      expect(reads()).toHaveLength(1);
      // while the worker has not deleted it yet, the case is listed, cannot be deleted twice and its flags rest
      const row = rowOf("Backend Engineer (Python)");
      expect(within(row).queryByRole("button", { name: /^Delete/ })).toBeNull();
      expect(within(row).getByText(en["golden.deletion_requested"])).toBeTruthy();
      expect(within(row).getAllByRole<HTMLInputElement>("checkbox").every((box) => box.disabled)).toBe(true);
      // once the deletion has run, the set is read again: the case is gone
      api.goldenCases = [];
      api.manifests = [manifestOut({ id: 9, kind: "golden_case_delete", status: "shipped" })];
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      await waitFor(() => {
        expect(requested?.textContent).toContain(en["settings_privacy.status.shipped"]);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });
      expect(reads()).toHaveLength(2);
      for (const read of reads()) {
        expect(read.headers.get("X-JH-Background")).toBe("1");
      }
      await waitFor(() => {
        expect(screen.queryByText("Backend Engineer (Python)", { selector: "td" })).toBeNull();
      });
      expect(requested?.isConnected).toBe(true); // the requested deletion stays listed with its state
    } finally {
      vi.useRealTimers();
    }
  });

  it("a confirmed delete hands the focus to the set's heading, as the row's Delete has gone", async () => {
    // plan M5 Task 33, the visual pass: the dialog gave the focus back to a Delete that was no longer there
    const api = fakeApi({ signedIn: true });
    api.goldenCases = [goldenCase()];
    api.routes.set("DELETE /api/golden-cases/1", () => json({ manifest_id: 9 }, 202));
    api.manifests = [manifestOut({ id: 9, kind: "golden_case_delete", status: "executing", completed_at: null, shipped_at: null })];
    const user = userEvent.setup();
    renderInProviders(<GoldenSet />);
    await screen.findByText("Backend Engineer (Python)", { selector: "td" });
    await user.click(within(rowOf("Backend Engineer (Python)")).getByRole("button", { name: /^Delete/ }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: en["golden.delete"] }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(within(rowOf("Backend Engineer (Python)")).getByText(en["golden.deletion_requested"])).toBeTruthy();
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByRole("heading", { name: en["settings_system.golden"] }));
    });
  });

  it("a requested deletion whose read is refused is not read again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const api = fakeApi({ signedIn: true });
      api.goldenCases = [goldenCase()];
      api.routes.set("DELETE /api/golden-cases/1", () => json({ manifest_id: 9 }, 202));
      // no manifest 9: its read answers 404, which no poll or retry changes
      renderInProviders(<GoldenSet />);
      fireEvent.click(within(await screen.findByRole("table")).getByRole("button", { name: /^Delete/ }));
      fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: en["golden.delete"] }));
      const status = await screen.findByRole("region", { name: en["golden.deletions"] });
      expect((await within(status).findByRole("alert")).textContent).toContain(en["problem.not_found"]);
      const reads = () => requestsTo(api, "GET /api/privacy/manifests/9");
      expect(reads()).toHaveLength(1);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expect(reads()).toHaveLength(1);
      // its Retry reads it again
      fireEvent.click(within(status).getByRole("button", { name: en["action.retry"] }));
      await waitFor(() => {
        expect(reads()).toHaveLength(2);
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("a refused delete shows by its code at the row", async () => {
    const api = fakeApi({ signedIn: true });
    api.goldenCases = [goldenCase()];
    api.routes.set("DELETE /api/golden-cases/1", () => problem(409, "conflict_state", { detail: "eval_run_running", title: "An eval run is running" }));
    renderInProviders(<GoldenSet />);
    fireEvent.click(within(await screen.findByRole("table")).getByRole("button", { name: /^Delete/ }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: en["golden.delete"] }));
    // in the row below the case's
    const alert = await screen.findByRole("alert");
    expect(alert.closest("tr")?.previousElementSibling).toBe(rowOf("Backend Engineer (Python)"));
    expect(alert.textContent).toContain(en["problem.conflict_state"]);
    expect(within(alert).getByText("eval_run_running").tagName).toBe("CODE");
    expect(alert.textContent).not.toContain("An eval run is running");
    expect(screen.queryByRole("region", { name: en["golden.deletions"] })).toBeNull();
  });
});
