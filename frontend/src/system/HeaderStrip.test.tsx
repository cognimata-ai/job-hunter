import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import de from "../i18n/de.json";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import { fakeApi, json, problem, requestsTo, statusOut } from "../test/api";
import { NOW, renderApp } from "../test/app";

afterEach(async () => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

/** The texts of the header's list items, the navigation's first. */
async function headerItems(): Promise<string[]> {
  const header = await screen.findByRole("banner");
  await within(header).findByText(en["header.last_run"]);
  return within(header).getAllByRole("listitem").map((li) => li.textContent);
}

describe("HeaderStrip", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"], now: NOW }); // the client's clock (the next slot's day); timers stay real
  });

  it("renders the three lines from a status", async () => {
    fakeApi({ signedIn: true });
    renderApp("/");
    const items = await headerItems();
    expect(items).toEqual([
      "Jobs",
      "Profile & Settings",
      // line 2: the age is the server's (queued_at + age_s), the next slot and the reset in Europe/Berlin
      "Last run 2 hours ago",
      "Partial (A fetch failed)",
      "Next 14:00",
      "JSearch 112/200 (resets 16 Nov)",
      // line 3
      "SerpApi 3/250",
      "Adzuna 41/2,250",
      "LLM $38.20 / $100.00 (pace ok)",
      "Coverage gaps 2",
      "Pending 212",
    ]);
    const header = screen.getByRole("banner");
    const facts = within(header).getAllByRole("listitem").slice(2);
    const [line2, line3] = [facts[0].parentElement, facts[4].parentElement];
    expect(facts.slice(0, 4).every((li) => li.parentElement === line2)).toBe(true);
    expect(facts.slice(4).every((li) => li.parentElement === line3)).toBe(true);
    expect(line2).not.toBe(line3);
    // line 1: the brand, the one navigation, Run now, the alerts and Logout
    expect(within(header).getByText(en["app.name"])).toBeTruthy();
    expect(screen.getAllByRole("navigation")).toHaveLength(1);
    expect(within(header).getByRole("button", { name: en["run_now.open"] })).toBeTruthy();
    expect(within(header).getByRole("button", { name: en["header.logout"] })).toBeTruthy();
  });

  it("names the partial causes", async () => {
    const api = fakeApi({ signedIn: true });
    const base = statusOut();
    if (base.last_run === null) {
      throw new Error("the fixture has a run");
    }
    api.status = {
      ...base,
      last_run: { ...base.last_run, partial_causes: ["fetch_failed", "source_breaker_open"] },
    };
    const app = renderApp("/");
    expect(await headerItems()).toContain("Partial (A fetch failed, A source is paused after failures)");
    api.status = {
      ...base,
      last_run: { ...base.last_run, run: { ...base.last_run.run, status: "succeeded" }, partial_causes: [] },
    };
    await act(async () => {
      await app.queryClient.refetchQueries({ queryKey: ["system", "status"] });
    });
    await waitFor(() => {
      expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toContain("Succeeded");
    });
  });

  it("shows pace and the reset date in the display time zone", async () => {
    const api = fakeApi({ signedIn: true });
    api.timezone = "Asia/Tokyo"; // UTC+9: the instants below fall on the next day there
    api.status = statusOut({
      next_slots: ["2026-11-03T20:30:00Z"], // 05:30 on 4 Nov in Tokyo, which is 16:00 on 3 Nov there now
      quota: {
        ...statusOut().quota,
        jsearch: { used: 200, limit: 200, reset_at: "2026-11-15T20:00:00Z", state: "exhausted_until_reset" },
      },
      llm: { counted: "61.500000", cap: "100.00", pace: "ahead" },
    });
    renderApp("/");
    const items = await headerItems();
    expect(items).toContain("Next 4 Nov, 05:30");
    expect(items).toContain("JSearch 200/200 (resets 16 Nov)");
    expect(items).toContain("LLM $61.50 / $100.00 (pace ahead)");
  });

  it("shows a dash for a provider without a window and no LLM item before seeding", async () => {
    const api = fakeApi({ signedIn: true });
    api.status = statusOut({ quota: { ...statusOut().quota, serpapi: null }, llm: null, next_slots: [] });
    renderApp("/");
    const items = await headerItems();
    expect(items).toContain("SerpApi —");
    expect(items.some((text) => text.startsWith("LLM"))).toBe(false);
    expect(items).toContain(en["header.next_none"]);
  });

  it("without runs shows the first run at the next slot", async () => {
    const api = fakeApi({ signedIn: true });
    api.status = statusOut({ last_run: null });
    renderApp("/");
    const header = await screen.findByRole("banner");
    const firstRun = await within(header).findByText(/First run at 14:00/);
    const item = firstRun.closest("li");
    expect(item?.textContent).toBe("First run at 14:00 — Run now");
    expect(within(header).queryByText(en["header.last_run"])).toBeNull();
    // its [Run now] opens the one popover of line 1's button
    const user = userEvent.setup();
    if (item === null) {
      throw new Error("no item");
    }
    await user.click(within(item).getByRole("button", { name: en["run_now.open"] }));
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    const trigger = within(header).getAllByRole("button", { name: en["run_now.open"] })[0];
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
  });

  it("the alerts count links to the System section with a text name", async () => {
    const api = fakeApi({ signedIn: true });
    api.status = statusOut({ alerts: { critical_unacked: 1, total_unacked: 3 } });
    renderApp("/");
    const link = await screen.findByRole("link", { name: "3 unacknowledged alerts, 1 critical" });
    expect(link.getAttribute("href")).toBe("/settings#system");
    expect(link.textContent).toContain("3");
    expect(link.textContent).toContain("1 critical"); // in text, not by its colour alone
  });

  it("names one alert in the singular, and in German", async () => {
    const api = fakeApi({ signedIn: true });
    api.uiLanguage = "de";
    renderApp("/");
    expect(await screen.findByRole("link", { name: "1 unquittierte Meldung, 0 kritisch" })).toBeTruthy();
    expect(screen.getByRole("button", { name: de["header.logout"] })).toBeTruthy();
  });

  it("keeps the navigation, Run now and Logout while the status cannot be read, with a retry", async () => {
    const api = fakeApi({ signedIn: true });
    let down = true;
    api.routes.set("GET /api/system/status", () => (down ? problem(404, "not_found") : json(statusOut())));
    renderApp("/");
    const header = await screen.findByRole("banner");
    const alert = await within(header).findByRole("alert");
    expect(screen.getByRole("navigation", { name: en["nav.label"] })).toBeTruthy();
    expect(within(header).getByRole("button", { name: en["run_now.open"] })).toBeTruthy();
    expect(within(header).getByRole("button", { name: en["header.logout"] })).toBeTruthy();
    down = false;
    await userEvent.setup().click(within(alert).getByRole("button", { name: en["action.retry"] }));
    expect(await within(header).findByText(en["header.last_run"])).toBeTruthy();
  });

  it("logout clears the cache and goes to /login", async () => {
    const api = fakeApi({ signedIn: true });
    const app = renderApp("/settings#budget");
    const header = await screen.findByRole("banner");
    await within(header).findByText(en["header.last_run"]);
    await userEvent.setup().click(within(header).getByRole("button", { name: en["header.logout"] }));
    await waitFor(() => {
      expect(app.router.state.location.pathname).toBe("/login");
    });
    expect(app.router.state.location.search).toEqual({}); // no redirect: the user chose to leave
    const logout = requestsTo(api, "POST /api/auth/logout");
    expect(logout).toHaveLength(1);
    expect(logout[0].headers.get("X-JH-Request")).toBe("1");
    await waitFor(() => {
      expect(app.queryClient.getQueryCache().getAll()).toHaveLength(0);
    });
    expect(await screen.findByRole("button", { name: en["login.submit"] })).toBeTruthy();
  });

  it("a logout the server cannot answer keeps the user signed in, with a toast", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/auth/logout", () => problem(503, "internal"));
    const app = renderApp("/");
    const header = await screen.findByRole("banner");
    await within(header).findByText(en["header.last_run"]);
    await userEvent.setup().click(within(header).getByRole("button", { name: en["header.logout"] }));
    expect(await screen.findByText(en["header.logout_failed"])).toBeTruthy();
    expect(app.router.state.location.pathname).toBe("/");
    expect(screen.getByRole("banner")).toBeTruthy();
  });
});

describe("HeaderStrip polling", () => {
  it("polls every 60 s with the background header", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: NOW });
    const api = fakeApi({ signedIn: true });
    renderApp("/");
    const header = await screen.findByRole("banner");
    await within(header).findByText(en["header.last_run"]);
    expect(requestsTo(api, "GET /api/system/status")).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(55_000);
    });
    expect(requestsTo(api, "GET /api/system/status")).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/system/status")).toHaveLength(2);
    });
    const [first, second] = requestsTo(api, "GET /api/system/status");
    expect(first.headers.get("X-JH-Background")).toBe("1");
    expect(second.headers.get("X-JH-Background")).toBe("1");
    // only the status is polled: the session read is not repeated
    expect(requestsTo(api, "GET /api/auth/session")).toHaveLength(1);
  });
});
