import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { alertRow, fakeApi, json, requestsTo, statusOut } from "../test/api";
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
});
