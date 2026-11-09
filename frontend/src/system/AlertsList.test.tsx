import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { alertRow, fakeApi, json, problem, requestsTo, statusOut } from "../test/api";
import { renderInProviders } from "../test/app";
import { AlertsList } from "./AlertsList";
import { STATUS_KEY } from "./api";

afterEach(() => {
  vi.restoreAllMocks();
});

function panel(): HTMLElement {
  return screen.getByRole("tabpanel");
}

describe("AlertsList", () => {
  it("tabs filter by status and Ack moves an alert", async () => {
    const api = fakeApi({ signedIn: true });
    api.alerts = [
      alertRow(),
      alertRow({ id: 2, code: "coverage.too_broad", severity: "info", status: "acknowledged", acknowledged_at: "2026-11-03T06:40:00Z", occurrences: 1 }),
      alertRow({ id: 1, code: "x.unknown_code", severity: "critical", message: "server text", status: "resolved", resolved_at: "2026-11-03T06:50:00Z" }),
    ];
    const user = userEvent.setup();
    renderInProviders(<AlertsList />);
    const tabs = within(await screen.findByRole("tablist", { name: en["settings_system.alerts_tabs"] })).getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual([en["alert_status.open"], en["alert_status.acknowledged"], en["alert_status.resolved"]]);

    // open: shown by its code's text, its severity, when it was first and last seen, and its count
    const open = await within(panel()).findByText(en["alert.quota.unaccounted_usage"]);
    const row = open.closest("li") as HTMLElement;
    expect(within(row).getByText(en["alert_severity.warn"])).toBeTruthy();
    expect(row.textContent).toContain("First 3 Nov, 06:00 · last 3 Nov, 07:30");
    expect(row.textContent).toContain("2 times");
    expect(within(panel()).queryByText(en["alert.coverage.too_broad"])).toBeNull();
    expect(requestsTo(api, "GET /api/alerts").map((r) => r.search.get("status"))).toEqual(["open"]);

    await user.click(tabs[1]);
    expect(await within(panel()).findByText(en["alert.coverage.too_broad"])).toBeTruthy();
    expect(within(panel()).queryByText(en["alert.quota.unaccounted_usage"])).toBeNull();
    expect(within(panel()).queryByRole("button", { name: en["alerts.ack"] })).toBeNull();

    await user.click(tabs[2]);
    // a code the catalogue lacks shows as its code, never the server's message
    expect(await within(panel()).findByText("x.unknown_code")).toBeTruthy();
    expect(within(panel()).queryByText("server text")).toBeNull();
    expect(requestsTo(api, "GET /api/alerts").map((r) => r.search.get("status"))).toEqual(["open", "acknowledged", "resolved"]);
  });

  it("Ack moves an alert out of the open tab", async () => {
    const api = fakeApi({ signedIn: true });
    api.alerts = [alertRow()];
    api.routes.set("POST /api/alerts/3/ack", () => {
      api.alerts = [alertRow({ status: "acknowledged", acknowledged_at: "2026-11-03T07:00:00Z" })];
      return json({ id: 3, acknowledged_at: "2026-11-03T07:00:00Z" });
    });
    const user = userEvent.setup();
    const app = renderInProviders(<AlertsList />);
    app.queryClient.setQueryData(STATUS_KEY, statusOut());
    const row = (await within(panel()).findByText(en["alert.quota.unaccounted_usage"])).closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: en["alerts.ack"] }));
    expect(requestsTo(api, "POST /api/alerts/3/ack")).toHaveLength(1);
    expect(requestsTo(api, "POST /api/alerts/3/ack")[0]?.headers.get("Idempotency-Key")).toBeNull();
    expect(await within(panel()).findByText(en["alerts.empty_open"])).toBeTruthy();
    expect(app.queryClient.getQueryState(STATUS_KEY)?.isInvalidated).toBe(true);
    expect(await screen.findByText(en["alerts.acknowledged"])).toBeTruthy();

    await user.click(screen.getByRole("tab", { name: en["alert_status.acknowledged"] }));
    expect(await within(panel()).findByText(en["alert.quota.unaccounted_usage"])).toBeTruthy();
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/alerts").filter((r) => r.search.get("status") === "open")).toHaveLength(2);
    });
  });

  it("shows Resolve only for manual alerts", async () => {
    const api = fakeApi({ signedIn: true });
    api.alerts = [
      alertRow(),
      alertRow({ id: 4, code: "run.failed", severity: "critical", manual: false }),
      alertRow({ id: 2, code: "operation.failed", status: "acknowledged", acknowledged_at: "2026-11-03T06:40:00Z" }),
      alertRow({ id: 1, code: "llm.model_mismatch", status: "resolved", resolved_at: "2026-11-03T06:50:00Z" }),
    ];
    const user = userEvent.setup();
    renderInProviders(<AlertsList />);
    const resolveIn = (row: HTMLElement) => within(row).queryByRole("button", { name: en["alerts.resolve"] });

    const manual = (await within(panel()).findByText(en["alert.quota.unaccounted_usage"])).closest("li") as HTMLElement;
    const auto = within(panel()).getByText(en["alert.run.failed"]).closest("li") as HTMLElement;
    expect(resolveIn(manual)).toBeTruthy();
    expect(resolveIn(auto)).toBeNull(); // it resolves itself
    expect(within(auto).getByRole("button", { name: en["alerts.ack"] })).toBeTruthy();

    // an acknowledged manual alert is still open on the server: it can be resolved
    await user.click(screen.getByRole("tab", { name: en["alert_status.acknowledged"] }));
    const acknowledged = (await within(panel()).findByText(en["alert.operation.failed"])).closest("li") as HTMLElement;
    expect(resolveIn(acknowledged)).toBeTruthy();

    await user.click(screen.getByRole("tab", { name: en["alert_status.resolved"] }));
    const resolved = (await within(panel()).findByText(en["alert.llm.model_mismatch"])).closest("li") as HTMLElement;
    expect(resolveIn(resolved)).toBeNull();
  });

  it("resolve asks for confirmation and refreshes the list", async () => {
    const api = fakeApi({ signedIn: true });
    api.alerts = [alertRow()];
    api.routes.set("POST /api/alerts/3/resolve", () => {
      const resolved = alertRow({ status: "resolved", resolved_at: "2026-11-03T07:00:00Z" });
      api.alerts = [resolved];
      return json(resolved);
    });
    const user = userEvent.setup();
    const app = renderInProviders(<AlertsList />);
    app.queryClient.setQueryData(STATUS_KEY, statusOut());
    const row = (await within(panel()).findByText(en["alert.quota.unaccounted_usage"])).closest("li") as HTMLElement;

    // Cancel sends nothing
    await user.click(within(row).getByRole("button", { name: en["alerts.resolve"] }));
    const asked = screen.getByRole("alertdialog", { name: en["alerts.resolve_title"] });
    expect(asked.textContent).toContain(en["alert.quota.unaccounted_usage"]);
    await user.click(within(asked).getByRole("button", { name: en["action.cancel"] }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(requestsTo(api, "POST /api/alerts/3/resolve")).toHaveLength(0);

    await user.click(within(row).getByRole("button", { name: en["alerts.resolve"] }));
    const dialog = screen.getByRole("alertdialog", { name: en["alerts.resolve_title"] });
    await user.click(within(dialog).getByRole("button", { name: en["alerts.resolve"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/alerts/3/resolve")).toHaveLength(1);
    });
    expect(requestsTo(api, "POST /api/alerts/3/resolve")[0]?.headers.get("Idempotency-Key")).toBeNull();
    expect(await screen.findByText(en["alerts.resolved"])).toBeTruthy();
    expect(await within(panel()).findByText(en["alerts.empty_open"])).toBeTruthy();
    expect(app.queryClient.getQueryState(STATUS_KEY)?.isInvalidated).toBe(true);

    await user.click(screen.getByRole("tab", { name: en["alert_status.resolved"] }));
    expect(await within(panel()).findByText(en["alert.quota.unaccounted_usage"])).toBeTruthy();
  });

  it("a not_manual refusal of Resolve shows in the row and the list is read again", async () => {
    const api = fakeApi({ signedIn: true });
    api.alerts = [alertRow()];
    api.routes.set("POST /api/alerts/3/resolve", () => {
      api.alerts = [alertRow({ manual: false })]; // the server resolves it itself after all
      return problem(409, "conflict_state", { detail: "not_manual" });
    });
    const user = userEvent.setup();
    renderInProviders(<AlertsList />);
    const row = (await within(panel()).findByText(en["alert.quota.unaccounted_usage"])).closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: en["alerts.resolve"] }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: en["alerts.resolve"] }));

    const refusal = await within(row).findByRole("alert");
    expect(within(refusal).getByText("not_manual").tagName).toBe("CODE");
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/alerts").filter((r) => r.search.get("status") === "open")).toHaveLength(2);
    });
    // read again: the alert is not the user's to resolve, so its Resolve goes; the refusal stays shown
    await waitFor(() => {
      expect(within(row).queryByRole("button", { name: en["alerts.resolve"] })).toBeNull();
    });
    expect(within(row).getByRole("alert")).toBe(refusal);
    expect(screen.queryByText(en["alerts.resolved"])).toBeNull();
  });

  it("a not_found answer to Resolve shows in the row and the list is read again", async () => {
    const api = fakeApi({ signedIn: true });
    api.alerts = [alertRow()];
    api.routes.set("POST /api/alerts/3/resolve", () => problem(404, "not_found"));
    const user = userEvent.setup();
    renderInProviders(<AlertsList />);
    const row = (await within(panel()).findByText(en["alert.quota.unaccounted_usage"])).closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: en["alerts.resolve"] }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: en["alerts.resolve"] }));

    const refusal = await within(row).findByRole("alert");
    expect(refusal.textContent).toContain(en["problem.not_found"]);
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/alerts").filter((r) => r.search.get("status") === "open")).toHaveLength(2);
    });
    expect(screen.queryByText(en["alerts.resolved"])).toBeNull();
  });

  it("shows delivery failures", async () => {
    const api = fakeApi({ signedIn: true });
    api.alerts = [
      alertRow({ id: 5, code: "quota.limit_breach", severity: "critical", last_notified_at: "2026-11-03T05:45:00Z" }),
      alertRow({ id: 4, code: "run.failed", severity: "critical", manual: false, notify_failures: 3 }),
      alertRow({ id: 6, code: "operation.failed", last_notified_at: "2026-11-03T05:50:00Z", notify_failures: 1 }),
      alertRow(),
    ];
    renderInProviders(<AlertsList />);
    const rowOf = (code: string) => within(panel()).getByText(en[`alert.${code}` as keyof typeof en]).closest("li") as HTMLElement;
    await within(panel()).findByText(en["alert.quota.limit_breach"]);

    expect(rowOf("quota.limit_breach").textContent).toContain("Notified 3 Nov, 06:45");
    expect(rowOf("quota.limit_breach").textContent).not.toContain("Delivery failed");
    expect(rowOf("run.failed").textContent).toContain("Delivery failed 3×");
    expect(rowOf("run.failed").textContent).not.toContain("Notified");
    // pushed once, and a push of it failed for good another time: both
    expect(rowOf("operation.failed").textContent).toContain("Notified 3 Nov, 06:50 · Delivery failed 1×");
    // not pushed yet, and nothing failed: no delivery state
    const quiet = rowOf("quota.unaccounted_usage").textContent;
    expect(quiet).not.toContain("Notified");
    expect(quiet).not.toContain("Delivery failed");
  });
});
