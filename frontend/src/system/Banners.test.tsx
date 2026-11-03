import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import { fakeApi, json, problem, requestsTo, statusOut } from "../test/api";
import { renderApp, settled } from "../test/app";

type BannerOut = components["schemas"]["BannerOut"];

afterEach(async () => {
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

const ROLES = "[role=alert], [role=status]";

/** The banner whose text matches `text`: the element that carries its role. */
async function banner(text: string | RegExp): Promise<HTMLElement> {
  const found = await screen.findByText(text);
  const element = found.closest<HTMLElement>(ROLES);
  if (element === null) {
    throw new Error(`no banner around ${String(text)}`);
  }
  return element;
}

function withBanners(banners: BannerOut[]) {
  const api = fakeApi({ signedIn: true });
  api.status = statusOut({ banners });
  return api;
}

describe("Banners", () => {
  it("one banner per code with its severity role", async () => {
    withBanners([
      { code: "stale_data", severity: "critical", params: {} },
      { code: "cap_reached", severity: "critical", params: {} },
      { code: "reservation_bound_violated", severity: "critical", params: {} },
      { code: "scoring_paused", severity: "critical", params: { reason: "auth_failed" } },
      { code: "critical_alerts", severity: "critical", params: { count: 2 } },
      { code: "paid_search_paused", severity: "warn", params: { until: "2026-11-15T23:30:00Z", reason: "serpapi_exhausted" } },
      { code: "rescoring", severity: "info", params: { done: 1200, total: 4000 } },
    ]);
    renderApp("/");
    const stale = await banner(en["banner.stale_data"]);
    expect(stale.getAttribute("role")).toBe("alert");
    expect((await banner("Unresolved critical alerts: 2")).getAttribute("role")).toBe("alert");
    expect((await banner("Scoring paused — LLM key refused")).getAttribute("role")).toBe("alert");
    // the instant in the display time zone (Europe/Berlin: 00:30 on 16 Nov), the reason translated
    const paid = await banner("Paid search paused until 16 Nov, 00:30 — SerpApi is used up too");
    expect(paid.getAttribute("role")).toBe("status");
    const rescoring = await banner("Re-scoring after profile change: 1,200 of 4,000");
    expect(rescoring.getAttribute("role")).toBe("status");
    // stacked in the order received
    const all = [...screen.getByRole("banner").querySelectorAll(ROLES)].map((b) => b.textContent);
    expect(all).toEqual([
      en["banner.stale_data"],
      `${en["banner.cap_reached"]} ${en["banner.needs_attention"]}`,
      `${en["banner.reservation_bound_violated"]} ${en["banner.needs_attention"]}`,
      `Scoring paused — LLM key refused ${en["banner.needs_attention"]}`,
      "Unresolved critical alerts: 2",
      "Paid search paused until 16 Nov, 00:30 — SerpApi is used up too",
      "Re-scoring after profile change: 1,200 of 4,000",
    ]);
    // the three pauses link to Needs attention, in the System section
    for (const text of [en["banner.cap_reached"], en["banner.reservation_bound_violated"], "Scoring paused — LLM key refused"]) {
      const link = within(await banner(text)).getByRole("link", { name: en["banner.needs_attention"] });
      expect(link.getAttribute("href")).toBe("/settings#system");
    }
    expect(within(stale).queryByRole("link")).toBeNull();
  });

  it("applying settings shows n of N in the live region", async () => {
    const api = withBanners([{ code: "applying_settings", severity: "info", params: { done: 3, total: 1500 } }]);
    const app = renderApp("/");
    const applying = await banner("Applying settings… 3/1,500");
    expect(applying.getAttribute("role")).toBe("status");
    api.status = statusOut({ banners: [{ code: "applying_settings", severity: "info", params: { done: 900, total: 1500 } }] });
    await act(async () => {
      await app.queryClient.refetchQueries({ queryKey: ["system", "status"] });
    });
    await waitFor(() => {
      expect(applying.textContent).toContain("Applying settings… 900/1,500");
    });
    // the same element, updated in place: the live region persists, so the change is announced
    expect(applying.isConnected).toBe(true);
    // an apply that has not reported yet (its counts are 0) shows no counts
    api.status = statusOut({ banners: [{ code: "applying_settings", severity: "info", params: { done: 0, total: 0 } }] });
    await act(async () => {
      await app.queryClient.refetchQueries({ queryKey: ["system", "status"] });
    });
    await waitFor(() => {
      expect(applying.textContent).toBe(en["banner.applying_settings_starting"]);
    });
    api.status = statusOut({ banners: [{ code: "applying_settings", severity: "info", params: { failed: true } }] });
    await act(async () => {
      await app.queryClient.refetchQueries({ queryKey: ["system", "status"] });
    });
    await waitFor(() => {
      expect(applying.textContent).toBe(en["banner.applying_settings_failed"]);
    });
    expect(within(applying).queryByRole("button")).toBeNull();
  });

  it("German texts", async () => {
    const api = withBanners([
      { code: "critical_alerts", severity: "critical", params: { count: 1234 } },
      { code: "applying_settings", severity: "info", params: { done: 2, total: 10 } },
    ]);
    api.uiLanguage = "de";
    renderApp("/");
    expect((await banner("Ungelöste kritische Meldungen: 1.234")).getAttribute("role")).toBe("alert");
    expect((await banner("Einstellungen werden angewendet … 2/10")).getAttribute("role")).toBe("status");
  });

  it("an unknown banner code shows its code", async () => {
    withBanners([
      { code: "deletion_unconfirmed" as BannerOut["code"], severity: "warn", params: { count: 1 } },
      { code: "rescoring", severity: "info", params: { done: 1, total: 2 } },
    ]);
    renderApp("/");
    expect((await banner("deletion_unconfirmed")).getAttribute("role")).toBe("status");
    expect(await banner("Re-scoring after profile change: 1 of 2")).toBeTruthy();
  });

  it("retry apply posts without an Idempotency-Key and refreshes the status", async () => {
    const api = withBanners([{ code: "scope_not_applied", severity: "warn", params: {} }]);
    api.routes.set("POST /api/settings/scope/retry-apply", () => {
      api.status = statusOut({ banners: [{ code: "applying_settings", severity: "info", params: { done: 0, total: 40 } }] });
      return json({ operation_id: 77 }, 202);
    });
    renderApp("/");
    const notApplied = await banner(en["banner.scope_not_applied"]);
    await userEvent.setup().click(within(notApplied).getByRole("button", { name: en["banner.retry_apply"] }));
    expect(await banner("Applying settings… 0/40")).toBeTruthy();
    const [post] = requestsTo(api, "POST /api/settings/scope/retry-apply");
    expect(post.headers.get("Idempotency-Key")).toBeNull();
    expect(post.headers.get("X-JH-Request")).toBe("1");
    expect(requestsTo(api, "GET /api/system/status").length).toBeGreaterThanOrEqual(2);
    await settled();
    expect(document.querySelector("[aria-live=polite][role=status]")?.textContent).toBe(en["banner.retry_apply_started"]);
  });

  it("retry apply shows a conflict in place", async () => {
    const api = withBanners([{ code: "scope_not_applied", severity: "warn", params: {} }]);
    api.routes.set("POST /api/settings/scope/retry-apply", () =>
      problem(409, "conflict_state", { detail: "an apply is live" }),
    );
    renderApp("/");
    const notApplied = await banner(en["banner.scope_not_applied"]);
    await userEvent.setup().click(within(notApplied).getByRole("button", { name: en["banner.retry_apply"] }));
    const shown = await within(notApplied).findByRole("alert");
    expect(shown.textContent).toContain(en["problem.conflict_state"]);
  });
});
