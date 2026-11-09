import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { fakeApi, requestsTo, spendOut } from "../test/api";
import { renderApp } from "../test/app";
import { dryRunPayloads } from "../test/settings";

// The section's Needs attention link is the router's `Link`: these tests render the app at Profile & Settings.
afterEach(() => {
  vi.restoreAllMocks();
});

function budget(): HTMLElement {
  const element = document.getElementById("budget");
  if (element === null) {
    throw new Error("no budget section");
  }
  return element;
}

/** The value beside the term `name` in the live figures. */
function figure(name: string): string {
  const term = within(budget()).getByText(name, { selector: "dt" });
  return term.nextElementSibling?.textContent ?? "";
}

describe("BudgetSection", () => {
  it("live spend, bucket, ĉ and quota windows", async () => {
    const api = fakeApi({ signedIn: true });
    api.spend = spendOut({
      bulk_operation: {
        id: 4,
        state: "active",
        budget_usd: "22.440000",
        spent_usd: "3.100000",
        followup_held_usd: "0.200000",
        llm_tuple_id: 2,
      },
      quarantined_batch_usd: "0.840000",
    });
    renderApp("/settings#budget");
    await waitFor(() => {
      expect(figure(en["settings_budget.counted"])).toBe("$38.20 of $100.00");
    });
    expect(figure(en["settings_budget.headroom"])).toBe("$61.80");
    expect(figure(en["settings_budget.bucket"])).toBe("$1.20 of $1.55, +$0.1293 an hour");
    expect(figure(en["settings_budget.c_hat"])).toBe(`$0.0526 · ${en["c_hat_source.history"]}`);
    expect(figure(en["settings_budget.bulk"])).toBe("$3.10 of $22.44");
    expect(figure(en["settings_budget.quarantined"])).toContain("$0.84");
    expect(within(budget()).getByRole("link", { name: en["banner.needs_attention"] }).getAttribute("href")).toBe("/settings#system");

    const table = within(budget()).getByRole("table", { name: en["settings_budget.quota"] });
    const rows = within(table).getAllByRole("row").slice(1).map((row) => [...row.children].map((cell) => cell.textContent));
    expect(rows).toEqual([
      ["JSearch", "112 of 200", "16 Nov", "2", "1", "0"],
      ["SerpApi", en["settings_budget.quota_none"]],
      ["Adzuna", "41 of 2,250", "1 Dec", "2", "1", "0"],
    ]);
    // one spend read (the Jobs list's query, read live here), in the background as a poll
    expect(requestsTo(api, "GET /api/system/spend")).toHaveLength(1);
    for (const read of [...requestsTo(api, "GET /api/system/spend"), ...requestsTo(api, "GET /api/system/quota")]) {
      expect(read.headers.get("X-JH-Background")).toBe("1");
    }
    // the live figures never enter the form
    expect(within(budget()).getByText(en["settings_form.no_changes"])).toBeTruthy();
  });

  it("edits the cap as a decimal string, with no bound written out", async () => {
    const api = fakeApi({ signedIn: true });
    renderApp("/settings#budget");
    const cap = await screen.findByRole("textbox", { name: en["settings_budget.cap"] });
    expect((cap as HTMLInputElement).value).toBe("100.00");
    fireEvent.change(cap, { target: { value: "120,50" } });
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.llm.cap_usd).toBe("120.50");
    });
    // the bounds are the dry-run's errors, never a copy of the model's ranges
    const most = screen.getByRole("spinbutton", { name: en["settings_budget.max_targets"] });
    expect([most.getAttribute("min"), most.getAttribute("max")]).toEqual([null, null]);
  });
});
