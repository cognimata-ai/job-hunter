import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { fakeApi } from "../test/api";
import { dryRunPayloads, renderSettings } from "../test/settings";
import { RETENTION_ROWS } from "./PrivacySection";

afterEach(() => {
  vi.restoreAllMocks();
});

function privacy(): HTMLElement {
  const element = document.getElementById("privacy");
  if (element === null) {
    throw new Error("no privacy section");
  }
  return element;
}

describe("PrivacySection", () => {
  it("retention rows and the score-history field", async () => {
    const api = fakeApi({ signedIn: true });
    renderSettings(["privacy"]);
    const table = await within(privacy()).findByRole("table", { name: en["settings_privacy.table"] });
    const rows = within(table).getAllByRole("row").slice(1);
    // the static rows of §20.1, in order, each what is kept and for how long
    expect(rows.map((row) => row.querySelector("th")?.textContent)).toEqual(
      RETENTION_ROWS.map((row) => en[`settings_privacy.row.${row}`]),
    );
    const fixed = rows.filter((row) => row.querySelector("input") === null);
    for (const row of fixed) {
      expect(row.querySelector("td")?.textContent.trim()).not.toBe("");
    }
    // the rules a summary would leave out or understate (Ruling T31-c; test_privacy_retention_rows.py holds every
    // row's periods to spec §20.1)
    const kept = (what: string) =>
      rows.find((row) => row.querySelector("th")?.textContent === what)?.querySelector("td")?.textContent ?? "";
    expect(kept("Raw provider response bodies")).toBe("30 days, longer while a newer response refers to the same body");
    expect(kept("Raw provider response records (metadata, body already removed)")).toBe("With their fetch request: 13 months");
    expect(kept("Backups")).toBe("Backup sets: 7 daily, 4 weekly and 3 monthly. Manifest snapshots: indefinitely");
    expect(kept("Backup set and backup run records")).toBe("13 months");
    expect(kept("Closed or stale jobs (not saved, applied or in an active golden case)")).toBe(
      "90 days after they closed, then tombstoned: their hashed keys and user-action timestamps are kept, their content deleted",
    );
    expect(kept("Superseded deterministic results (det_results instances)")).toBe(
      "With the score history; the current instance follows its result",
    );

    // the one editable row: the score history, with the model's bounds
    const field = within(table).getByRole<HTMLInputElement>("spinbutton", { name: en["settings_privacy.score_history_months"] });
    expect(field.value).toBe("12");
    expect([field.min, field.max]).toEqual(["1", "36"]);
    expect(field.closest("tr")?.querySelector("th")?.textContent).toBe(en["settings_privacy.row.score_history"]);
    fireEvent.change(field, { target: { value: "18" } });
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.retention.score_history_months).toBe(18);
    });
    expect(within(privacy()).getByRole("button", { name: en["settings_form.save"] })).toBeTruthy();

    // the privacy statement as text (the SPA serves no docs), and what comes later, without a milestone's name
    expect(within(privacy()).getByText(en["settings_privacy.statement"])).toBeTruthy();
    expect(within(privacy()).queryByRole("link", { name: /PRIVACY/ })).toBeNull();
    expect(within(privacy()).getByText(en["settings_privacy.later"])).toBeTruthy();
    expect(privacy().textContent).not.toMatch(/\bM5\b/);
    expect(screen.queryByText(/M5/)).toBeNull();
  });
});
