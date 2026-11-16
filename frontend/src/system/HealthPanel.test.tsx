import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { fakeApi, healthOut } from "../test/api";
import { renderInProviders } from "../test/app";
import { HealthPanel } from "./HealthPanel";
import styles from "./HealthPanel.module.css";

afterEach(() => {
  vi.restoreAllMocks();
});

/** The value element beside the term `term` in the group `group`. */
function entry(group: string, term: string): Element {
  const region = screen.getByRole("region", { name: group });
  const dd = within(region).getByText(term, { selector: "dt" }).nextElementSibling;
  if (dd === null) {
    throw new Error(`no value for ${term}`);
  }
  return dd;
}

/** The value beside the term `term` in the group `group`. */
function value(group: string, term: string): string {
  return entry(group, term).textContent;
}

/** The panel rendered on a fresh fake api whose health is `health`, once its first group is shown. */
async function shown(health = healthOut()): Promise<void> {
  cleanup();
  fakeApi({ signedIn: true }).health = health;
  renderInProviders(<HealthPanel />);
  await screen.findByRole("region", { name: en["health.readiness"] });
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

  it("shows the backup block and its states", async () => {
    const group = en["health.backups"];
    await shown();
    expect(value(group, en["health.latest_set"])).toBe(`#12 · ${en["run_status.succeeded"]} · 8 hrs 20 mins ago`);
    expect(value(group, en["health.backup_age"])).toBe(en["backup_age_state.ok"]);
    expect(value(group, en["health.latest_shipment"])).toBe("#4 · 3 manifests · 10 Nov, 03:12");
    expect(value(group, en["health.unshipped"])).toBe("0");
    expect(value(group, en["health.latest_restore_test"])).toBe(`#3 · ${en["run_status.succeeded"]} · 9 days ago`);
    expect(value(group, en["health.restore_test_age"])).toBe(en["restore_test_age_state.ok"]);
    for (const term of ["health.latest_set", "health.backup_age", "health.latest_restore_test", "health.restore_test_age"] as const) {
      expect(entry(group, en[term]).classList.contains(styles.critical), term).toBe(false);
      expect(entry(group, en[term]).classList.contains(styles.warn), term).toBe(false);
    }

    await shown(healthOut({
      backups: {
        latest_set: { id: 13, status: "failed", finished_at: "2026-11-10T08:10:00Z", age_s: 7_200 },
        backup_age_state: "critical",
        latest_shipment: { id: 4, shipped_at: "2026-11-09T02:12:00Z", manifests: 1 },
        unshipped_manifests: 2,
        oldest_unshipped_completed_at: "2026-11-08T21:30:00Z",
        latest_restore_test: { id: 5, status: "running", finished_at: null, age_s: null },
        restore_test_age_state: "warn",
      },
    }));
    expect(value(group, en["health.latest_set"])).toBe(`#13 · ${en["run_status.failed"]} · 2 hrs ago`);
    expect(entry(group, en["health.latest_set"]).classList.contains(styles.critical)).toBe(true);
    expect(value(group, en["health.backup_age"])).toBe(en["backup_age_state.critical"]);
    expect(entry(group, en["health.backup_age"]).classList.contains(styles.critical)).toBe(true);
    expect(value(group, en["health.latest_shipment"])).toBe("#4 · 1 manifest · 9 Nov, 03:12");
    expect(value(group, en["health.unshipped"])).toBe("2 · oldest 8 Nov, 22:30");
    expect(value(group, en["health.latest_restore_test"])).toBe(`#5 · ${en["run_status.running"]}`);
    expect(value(group, en["health.restore_test_age"])).toBe(en["restore_test_age_state.warn"]);
    expect(entry(group, en["health.restore_test_age"]).classList.contains(styles.warn)).toBe(true);

    await shown(healthOut({
      backups: {
        latest_set: null,
        backup_age_state: "grace",
        latest_shipment: null,
        unshipped_manifests: 0,
        oldest_unshipped_completed_at: null,
        latest_restore_test: { id: 6, status: "failed", finished_at: "2026-11-10T09:10:00Z", age_s: 3_600 },
        restore_test_age_state: "none",
      },
    }));
    expect(value(group, en["health.latest_set"])).toBe(en["value.none"]);
    expect(value(group, en["health.backup_age"])).toBe(en["backup_age_state.grace"]);
    expect(value(group, en["health.latest_shipment"])).toBe(en["value.none"]);
    expect(value(group, en["health.latest_restore_test"])).toBe(`#6 · ${en["run_status.failed"]} · 1 hr ago`);
    expect(entry(group, en["health.latest_restore_test"]).classList.contains(styles.critical)).toBe(true);
    expect(value(group, en["health.restore_test_age"])).toBe(en["restore_test_age_state.none"]);
  });

  it("shows tracing on (EU)", async () => {
    await shown(healthOut({ langsmith: { tracing: true, region: "eu" } }));
    // the region through its namespace, as every closed set of the panel
    expect(value(en["health.readiness"], en["health.tracing"])).toBe(`On (${en["tracing_region.eu"]})`);
    await shown(healthOut({ langsmith: { tracing: true, region: null } }));
    expect(value(en["health.readiness"], en["health.tracing"])).toBe(en["health.tracing_on"]);
    // off as the worker published it, and before any worker published its state
    for (const langsmith of [{ tracing: false, region: null }, null] as const) {
      await shown(healthOut({ langsmith }));
      expect(value(en["health.readiness"], en["health.tracing"])).toBe(en["health.tracing_off"]);
    }
  });
});
