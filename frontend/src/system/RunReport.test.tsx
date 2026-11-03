import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { components } from "../api/schema";
import de from "../i18n/de.json";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import { RunReport } from "./RunReport";

type RunOut = components["schemas"]["RunOut"];

afterEach(async () => {
  await i18n.changeLanguage("en");
});

const RUN: RunOut = {
  id: 12,
  kind: "collect",
  trigger: "scheduled",
  status: "partial",
  status_reason: "fetch_failed",
  scheduled_slot: "2026-11-03T05:00:00Z",
  include_paid: true,
  attempt_no: 1,
  queued_at: "2026-11-03T05:00:00Z",
  started_at: "2026-11-03T05:00:01Z",
  finished_at: "2026-11-03T05:02:30Z",
};

const REPORT = {
  // not in §17.3's order, and with a key outside the five sections, which is not rendered
  execution: { alerts: ["run.partial:collect:fetch_failed", "quota.fallback_active:provider:serpapi"], warnings: [] },
  secret: { never: "<b>shown</b>" },
  quota: { jsearch: { used: 4, projected: 2, window_start: "2026-11-03T05:00:00+00:00" }, fallback: { used: false, cells: 0, reason: null } },
  sources: {
    "jsearch/default": { requests: { succeeded: 2, failed: 1 }, units: { billed: 2, ambiguous: 0 }, health: "ok" },
    "greenhouse/instance:5": { items: { linked: 1500 }, availability: {} },
  },
  identity: { jobs_created: 1234, merges: 0 },
  revisions: { created: 1234, embedded: 0.5, targets: { skipped: {}, by_state: { pending: 12 } } },
};

function sectionTitles(): string[] {
  return screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
}

/** The value the definition list `within` pairs with the term `term`. */
function valueOf(scope: HTMLElement, term: string): HTMLElement {
  const dt = within(scope).getAllByText(term, { selector: "dt" })[0];
  const dd = dt.nextElementSibling;
  if (!(dd instanceof HTMLElement) || dd.tagName !== "DD") {
    throw new Error(`no value for ${term}`);
  }
  return dd;
}

describe("RunReport", () => {
  it("renders partial causes and section counts", () => {
    render(<RunReport run={RUN} report={REPORT} partialCauses={["fetch_failed", "source_breaker_open"]} />);
    const root = document.body;
    expect(valueOf(root, en["run_report.status"]).textContent).toBe(en["run_status.partial"]);
    const reason = valueOf(root, en["run_report.reason"]);
    expect(reason.textContent).toBe("fetch_failed");
    expect(reason.querySelector("code")?.textContent).toBe("fetch_failed"); // a code, verbatim
    // the times in the display time zone (Europe/Berlin, UTC+1 in November)
    expect(valueOf(root, en["run_report.queued_at"]).textContent).toBe("3 Nov 2026, 06:00:00");
    expect(valueOf(root, en["run_report.finished_at"]).textContent).toBe("3 Nov 2026, 06:02:30");
    const causes = within(valueOf(root, en["run_report.partial_causes"])).getAllByRole("listitem");
    expect(causes.map((li) => li.textContent)).toEqual([en["partial.fetch_failed"], en["partial.source_breaker_open"]]);

    // the five sections in §17.3's order, translated; nothing outside them
    expect(sectionTitles()).toEqual([
      en["run_report.section.sources"],
      en["run_report.section.identity"],
      en["run_report.section.revisions"],
      en["run_report.section.quota"],
      en["run_report.section.execution"],
    ]);
    expect(screen.queryByText("secret")).toBeNull();
    expect(screen.queryByText(/shown/)).toBeNull();

    const sources = screen.getByRole("region", { name: en["run_report.section.sources"] });
    const jsearch = valueOf(sources, "jsearch/default"); // inner keys verbatim: source keys and counter names
    expect(valueOf(jsearch, "requests").querySelector("dl")).not.toBeNull(); // nested objects nest
    expect(valueOf(valueOf(jsearch, "requests"), "failed").textContent).toBe("1");
    expect(valueOf(jsearch, "health").textContent).toBe("ok");
    const greenhouse = valueOf(sources, "greenhouse/instance:5");
    expect(valueOf(valueOf(greenhouse, "items"), "linked").textContent).toBe("1,500");
    expect(valueOf(greenhouse, "availability").textContent).toBe("—");

    const identity = screen.getByRole("region", { name: en["run_report.section.identity"] });
    expect(valueOf(identity, "jobs_created").textContent).toBe("1,234");
    expect(valueOf(identity, "merges").textContent).toBe("0");
    const revisions = screen.getByRole("region", { name: en["run_report.section.revisions"] });
    expect(valueOf(revisions, "embedded").textContent).toBe("0.5");
    expect(valueOf(valueOf(valueOf(revisions, "targets"), "by_state"), "pending").textContent).toBe("12");

    const quota = screen.getByRole("region", { name: en["run_report.section.quota"] });
    const fallback = valueOf(quota, "fallback");
    expect(valueOf(fallback, "used").textContent).toBe(en["value.no"]);
    expect(valueOf(fallback, "reason").textContent).toBe("—");
    expect(valueOf(valueOf(quota, "jsearch"), "window_start").textContent).toBe("2026-11-03T05:00:00+00:00");

    const execution = screen.getByRole("region", { name: en["run_report.section.execution"] });
    expect(valueOf(execution, "alerts").textContent).toBe(
      "run.partial:collect:fetch_failed, quota.fallback_active:provider:serpapi",
    );
    expect(valueOf(execution, "warnings").textContent).toBe("—");
  });

  it("says when the report will appear, and shows no causes as a dash", () => {
    render(<RunReport run={{ ...RUN, status: "running", status_reason: null, finished_at: null }} report={null} partialCauses={[]} />);
    expect(screen.getByText(en["run_report.no_report"])).toBeTruthy();
    expect(screen.queryAllByRole("heading", { level: 3 })).toHaveLength(0);
    expect(valueOf(document.body, en["run_report.finished_at"]).textContent).toBe("—");
    expect(valueOf(document.body, en["run_report.partial_causes"]).textContent).toBe("—");
    expect(screen.queryByText(en["run_report.reason"])).toBeNull();
  });

  it("is translated, numbers and booleans included", async () => {
    await i18n.changeLanguage("de");
    render(<RunReport run={RUN} report={REPORT} partialCauses={["fetch_failed"]} />);
    expect(screen.getByRole("heading", { level: 3, name: de["run_report.section.identity"] })).toBeTruthy();
    const identity = screen.getByRole("region", { name: de["run_report.section.identity"] });
    expect(valueOf(identity, "jobs_created").textContent).toBe("1.234");
    expect(valueOf(document.body, de["run_report.queued_at"]).textContent).toBe("03.11.2026, 06:00:00");
    const quota = screen.getByRole("region", { name: de["run_report.section.quota"] });
    expect(valueOf(valueOf(quota, "fallback"), "used").textContent).toBe(de["value.no"]);
  });
});
