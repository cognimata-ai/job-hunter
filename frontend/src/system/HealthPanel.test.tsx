import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { fakeApi, healthOut } from "../test/api";
import { renderInProviders } from "../test/app";
import { HealthPanel } from "./HealthPanel";

afterEach(() => {
  vi.restoreAllMocks();
});

/** The value beside the term `term` in the group `group`. */
function value(group: string, term: string): string {
  const region = screen.getByRole("region", { name: group });
  const dt = within(region).getByText(term, { selector: "dt" });
  return dt.nextElementSibling?.textContent ?? "";
}

describe("HealthPanel", () => {
  it("shows the health as it comes, with ages and sizes in units and versions verbatim", async () => {
    fakeApi({ signedIn: true });
    renderInProviders(<HealthPanel />);
    await screen.findByRole("region", { name: en["health.readiness"] });
    expect(value(en["health.readiness"], en["health.state"])).toBe(en["health.ready"]);
    expect(value(en["health.readiness"], en["health.embedder"])).toBe(`${en["health.embedder_ok"]} · ${en["health.checksum_ok"]}`);
    expect(value(en["health.runs"], en["health.last_run"])).toBe(`#7 · ${en["run_status.succeeded"]} · 2 hrs ago`);
    expect(value(en["health.runs"], en["health.last_success"])).toBe("1 hr 58 mins ago");
    expect(value(en["health.runs"], en["health.run_age"])).toBe(en["run_age_state.ok"]);
    expect(value(en["health.queues"], en["queue.score"])).toBe(`${en["queue_status.todo"]} 1 · ${en["queue_status.doing"]} 0`);
    expect(value(en["health.score_lane"], en["health.pause"])).toBe(en["health.not_paused"]);
    expect(value(en["health.score_lane"], en["health.oldest_pending"])).toBe("1 hr 30 mins");
    expect(value(en["health.llm"], en["health.month"])).toBe("November 2026");
    expect(value(en["health.llm"], en["settings_budget.counted"])).toBe("$38.20 of $100.00");
    expect(value(en["health.quota"], "JSearch")).toBe(`112 of 200 · ${en["window_state.validated"]} · until 1 Dec`);
    expect(value(en["health.quota"], "SerpApi")).toBe(en["settings_budget.quota_none"]);
    expect(value(en["health.alerts"], en["alert_severity.warn"])).toBe("2");
    expect(value(en["health.sizes"], en["health.database"])).toBe("58.7 MB");
    const sizes = screen.getByRole("region", { name: en["health.sizes"] });
    expect(within(sizes).getByText("12.5 GB free of 250 GB")).toBeTruthy();
    expect(within(sizes).getByText("jobs").tagName).toBe("CODE");
    const versions = screen.getByRole("region", { name: en["health.versions"] });
    expect(within(versions).getByText("3.13.16").tagName).toBe("CODE");
    expect(within(versions).getByText(en["health.version.app"])).toBeTruthy();
  });

  it("names the failing readiness probes and a paused score lane", async () => {
    const api = fakeApi({ signedIn: true });
    api.health = healthOut({
      ready: { ok: false, failing: ["db", "embedder"] },
      scoring: { ...healthOut().scoring, pause_reason: "cap_reached", running_cycle_id: 12 },
      run_age_state: "critical",
    });
    renderInProviders(<HealthPanel />);
    await screen.findByRole("region", { name: en["health.readiness"] });
    expect(value(en["health.readiness"], en["health.state"])).toBe(`Not ready: ${en["readiness.db"]}, ${en["readiness.embedder"]}`);
    expect(value(en["health.score_lane"], en["health.pause"])).toBe(en["pause.cap_reached"]);
    expect(value(en["health.score_lane"], en["health.running_cycle"])).toBe("#12");
    expect(value(en["health.runs"], en["health.run_age"])).toBe(en["run_age_state.critical"]);
  });
});
