import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { fakeApi, json, manifestOut } from "../test/api";
import { renderApp } from "../test/app";
import { dryRunPayloads } from "../test/settings";
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

/** The Privacy & data section once its settings are read (its body renders then). */
async function section(): Promise<HTMLElement> {
  return waitFor(() => {
    const element = privacy();
    within(element).getByRole("table", { name: en["settings_privacy.table"] });
    return element;
  });
}

describe("PrivacySection", () => {
  it("retention rows and the score-history field", async () => {
    const api = fakeApi({ signedIn: true });
    renderApp("/settings#privacy");
    const table = await within(await section()).findByRole("table", { name: en["settings_privacy.table"] });
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
    expect(kept("Backups")).toBe(
      "Backup sets: 7 daily, 4 weekly and 3 monthly. Manifest snapshots: the newest 10, which hold every manifest shipped, so the manifests are kept indefinitely",
    );
    expect(kept("Backup set and backup run records")).toBe(
      "13 months after they finished, except the newest successful backup set and restore test, and a set a kept run refers to",
    );
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

    // the backup lag note, and a link to the privacy statement the page renders, without a milestone's name
    expect(within(privacy()).getByText(en["settings_privacy.later"])).toBeTruthy();
    const link = within(privacy()).getByRole("link", { name: en["settings_privacy.statement"] });
    expect(link.getAttribute("href")).toBe("/settings#privacy-statement");
    expect(document.getElementById("privacy-statement")).not.toBeNull();
    expect(privacy().textContent).not.toMatch(/\bM5\b|PRIVACY\.md|later version/);
    expect(screen.queryByText(/M5/)).toBeNull();
  });

  it("holds the actions, then the deletions, then the retention table", async () => {
    fakeApi({ signedIn: true }).manifests = [manifestOut()];
    renderApp("/settings#privacy");
    const body = await section();
    const actions = await within(body).findByRole("region", { name: en["settings_privacy.actions"] });
    const deletions = await within(body).findByRole("region", { name: en["settings_privacy.deletions"] });
    const table = await within(body).findByRole("table", { name: en["settings_privacy.table"] });
    for (const name of ["settings_privacy.export.start", "settings_privacy.purge.open", "settings_privacy.delete.open"] as const) {
      expect(within(actions).getByRole("button", { name: en[name] })).toBeTruthy();
    }
    expect(within(deletions).getByText(en["settings_privacy.status.shipped"])).toBeTruthy();
    const order = (a: Node, b: Node) => a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING;
    expect(order(actions, deletions)).toBeTruthy();
    expect(order(deletions, table)).toBeTruthy();
  });

  it("after delete everything its status shows, and after the logout and a new login it is found again", async () => {
    const api = fakeApi({ signedIn: true, loginAnswers: [() => new Response(null, { status: 204 })] });
    api.routes.set("POST /api/privacy/delete-everything", () => {
      api.manifests = [manifestOut({ id: 51, status: "executing", completed_at: null, shipped_at: null })];
      return json({ manifest_id: 51 }, 202);
    });
    const app = renderApp("/settings#privacy");
    const body = await section();
    const deletions = await within(body).findByRole("region", { name: en["settings_privacy.deletions"] });
    expect(await within(deletions).findByText(en["settings_privacy.no_deletions"])).toBeTruthy();
    fireEvent.click(await within(body).findByRole("button", { name: en["settings_privacy.delete.open"] }));
    const dialog = screen.getByRole("alertdialog", { name: en["settings_privacy.delete.dialog_title"] });
    fireEvent.change(within(dialog).getByLabelText(en["settings_privacy.phrase_label"]), { target: { value: "DELETE EVERYTHING" } });
    fireEvent.change(within(dialog).getByLabelText(en["login.password"]), { target: { value: "correct horse" } });
    fireEvent.click(within(dialog).getByRole("button", { name: en["settings_privacy.delete.submit"] }));
    // the deletion shows at once, executing
    await waitFor(() => {
      expect(within(deletions).getByText(en["settings_privacy.status.executing"])).toBeTruthy();
    });

    // it completes and ends the session: the next read leads to the login
    api.manifests = [manifestOut({ id: 51, status: "completed", shipped_at: null })];
    api.signedIn = false;
    await act(async () => {
      await app.queryClient.refetchQueries({ queryKey: ["privacy", "manifests"] });
    });
    await waitFor(() => {
      expect(app.router.state.location.pathname).toBe("/login");
    });
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(en["login.username"]), "smoke");
    await user.type(screen.getByLabelText(en["login.password"]), "correct horse");
    await user.click(screen.getByRole("button", { name: en["login.submit"] }));
    await waitFor(() => {
      expect(app.router.state.location.href).toBe("/settings#privacy");
    });
    // the list finds it again, not yet confirmed offsite
    const again = await within(await section()).findByRole("region", { name: en["settings_privacy.deletions"] });
    expect(await within(again).findByText(en["settings_privacy.status.completed"])).toBeTruthy();
  });
});
