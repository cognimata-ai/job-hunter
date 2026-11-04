import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { fakeApi, json, requestsTo, runRow, runsPage } from "../test/api";
import { renderInProviders } from "../test/app";
import { RunsList } from "./RunsList";

afterEach(() => {
  vi.restoreAllMocks();
});

const short = (unit: string, value: number) =>
  new Intl.NumberFormat("en-GB", { style: "unit", unit, unitDisplay: "short" }).format(value);

function table(): HTMLElement {
  return screen.getByRole("table", { name: en["settings_system.runs"] });
}

function rowOf(id: number): HTMLElement {
  const row = within(table()).getByText(`#${String(id)}`).closest("tr");
  if (row === null) {
    throw new Error(`no row for run ${String(id)}`);
  }
  return row;
}

describe("RunsList", () => {
  it("pages and opens a report", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/runs", (request) =>
      json(
        request.search.get("cursor") === "c1"
          ? runsPage([runRow({ id: 7, trigger: "manual", include_paid: false, scheduled_slot: null })])
          : runsPage(
              [
                runRow({ id: 9, status: "running", started_at: "2026-11-03T12:00:01Z", finished_at: null, queued_at: "2026-11-03T12:00:00Z" }),
                runRow({ id: 8, status: "partial", status_reason: "source_breaker_open" }),
              ],
              "c1",
            ),
      ),
    );
    api.routes.set("GET /api/runs/8", () =>
      json({
        run: runRow({ id: 8, status: "partial", status_reason: "source_breaker_open" }),
        report: { sources: { arbeitnow: { fetched: 12 } }, execution: { partial: ["source_breaker_open"] } },
        partial_causes: ["source_breaker_open"],
      }),
    );
    renderInProviders(<RunsList />);
    await screen.findByText("#9");
    expect(requestsTo(api, "GET /api/runs")[0]?.search.get("limit")).toBe("20");
    const cells = (id: number) => [...rowOf(id).querySelectorAll("td")].map((cell) => cell.textContent);
    // status, trigger, started and duration (a running run has none yet)
    expect(cells(9).slice(0, 4)).toEqual([en["run_status.running"], `${en["run_trigger.schedule"]} · ${en["runs.paid"]}`, "3 Nov 2026, 13:00", en["runs.no_duration"]]);
    expect(cells(8).slice(0, 4)).toEqual([`${en["run_status.partial"]}source_breaker_open`, `${en["run_trigger.schedule"]} · ${en["runs.paid"]}`, "3 Nov 2026, 07:00", `${short("minute", 2)} ${short("second", 5)}`]);

    fireEvent.click(screen.getByRole("button", { name: en["jobs.load_more"] }));
    await within(table()).findByText("#7");
    expect(requestsTo(api, "GET /api/runs").map((r) => r.search.get("cursor"))).toEqual([null, "c1"]);
    expect(cells(7).slice(1, 2)).toEqual([en["run_trigger.manual"]]);
    expect(screen.queryByRole("button", { name: en["jobs.load_more"] })).toBeNull();

    // a row's Report reads that run and shows its report below the row
    const report = within(rowOf(8)).getByRole("button", { name: /^Report/ });
    expect(report.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(report);
    expect(report.getAttribute("aria-expanded")).toBe("true");
    expect(await screen.findByRole("heading", { name: en["run_report.section.sources"] })).toBeTruthy();
    expect(screen.getByText(en["partial.source_breaker_open"])).toBeTruthy();
    expect(requestsTo(api, "GET /api/runs/8")).toHaveLength(1);
  });

  it("only one run report is open", async () => {
    const api = fakeApi({ signedIn: true });
    api.runs = runsPage([runRow({ id: 9 }), runRow({ id: 8 })]);
    api.routes.set("GET /api/runs/9", () => json({ run: runRow({ id: 9 }), report: { sources: {} }, partial_causes: [] }));
    api.routes.set("GET /api/runs/8", () => json({ run: runRow({ id: 8 }), report: { identity: {} }, partial_causes: [] }));
    renderInProviders(<RunsList />);
    await screen.findByText("#9");
    fireEvent.click(within(rowOf(9)).getByRole("button", { name: /^Report/ }));
    expect(await screen.findByRole("heading", { name: en["run_report.section.sources"] })).toBeTruthy();
    fireEvent.click(within(rowOf(8)).getByRole("button", { name: /^Report/ }));
    expect(await screen.findByRole("heading", { name: en["run_report.section.identity"] })).toBeTruthy();
    await waitFor(() => {
      expect(screen.queryByRole("heading", { name: en["run_report.section.sources"] })).toBeNull();
    });
    expect(within(rowOf(9)).getByRole("button", { name: /^Report/ }).getAttribute("aria-expanded")).toBe("false");
    expect(within(rowOf(8)).getByRole("button", { name: /^Report/ }).getAttribute("aria-expanded")).toBe("true");
    // Report again closes it
    fireEvent.click(within(rowOf(8)).getByRole("button", { name: /^Report/ }));
    expect(screen.queryByRole("heading", { name: en["run_report.section.identity"] })).toBeNull();
  });
});
