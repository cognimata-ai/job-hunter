import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { fakeApi, goldenCase, json, problem, requestsTo } from "../test/api";
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
      return new Response(null, { status: 204 });
    });
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
      expect(screen.queryByText("Softwareentwickler Java")).toBeNull();
    });
    expect(screen.getByText(en["golden.deleted"])).toBeTruthy();
  });
});
