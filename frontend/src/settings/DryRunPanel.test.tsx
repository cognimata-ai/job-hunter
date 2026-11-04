import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import { type FakeApi, dryRunOut, fakeApi, json, settingsRead } from "../test/api";
import { dryRunPayloads, renderSettings } from "../test/settings";

type Settings = components["schemas"]["Settings"];
type DryRunOut = components["schemas"]["DryRunOut"];

afterEach(() => {
  vi.restoreAllMocks();
});

/** Answers the base document's dry-run with `base`, any other with `draft`. */
function dryRuns(api: FakeApi, draft: DryRunOut, base: DryRunOut = dryRunOut()) {
  const basePayload = JSON.stringify(settingsRead().payload);
  api.routes.set("POST /api/settings/dry-run", (request) => {
    const { payload } = JSON.parse(request.body ?? "{}") as { payload: Settings };
    return json(JSON.stringify(payload) === basePayload ? base : draft);
  });
}

function section(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (element === null) {
    throw new Error(`no section ${id}`);
  }
  return element;
}

async function change(): Promise<void> {
  const interval = await screen.findByRole("spinbutton", { name: en["settings_schedule.interval"] });
  fireEvent.change(interval, { target: { value: "8" } });
}

function saveIn(id: string): HTMLButtonElement {
  return within(section(id)).getByRole<HTMLButtonElement>("button", { name: en["settings_form.save"] });
}

function warningIn(id: string, code: keyof typeof en): HTMLElement {
  const item = within(section(id)).getByText(en[code]).closest("li");
  if (item === null) {
    throw new Error(`no warning ${code}`);
  }
  return item;
}

const OUT_OF_RANGE = { field: "payload.schedule.interval_hours", code: "out_of_range" as const, message: "must be from 1 to 168" };

describe("DryRunPanel", () => {
  it("errors block Save and are linked to their fields", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, dryRunOut({ errors: [OUT_OF_RANGE], projections: null, effects: [] }));
    renderSettings(["scope", "schedule"]);
    await change();
    const interval = screen.getByRole("spinbutton", { name: en["settings_schedule.interval"] });
    await waitFor(() => {
      expect(interval.getAttribute("aria-invalid")).toBe("true");
    });
    const described = (interval.getAttribute("aria-describedby") ?? "").split(" ").map((id) => document.getElementById(id));
    const error = described.find((element) => element?.textContent.includes(en["issue.out_of_range"]));
    expect(error?.textContent).toContain("must be from 1 to 168");
    expect(section("schedule").contains(error ?? null)).toBe(true); // the error in its own section's panel
    expect(saveIn("schedule").disabled).toBe(true);
    expect(saveIn("scope").disabled).toBe(true);
    // the error's link takes focus to its field
    await userEvent.setup().click(within(section("schedule")).getByRole("link", { name: /must be from 1 to 168/ }));
    expect(document.activeElement).toBe(interval);
    // no projection with errors
    expect(within(section("schedule")).queryByText(en["dry_run.llm"])).toBeNull();
  });

  it("an error in another section links to its anchor", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, dryRunOut({ errors: [OUT_OF_RANGE], projections: null, effects: [] }));
    renderSettings(["scope", "schedule"]);
    await change();
    const link = await within(section("scope")).findByRole("link", { name: /must be from 1 to 168/ });
    expect(link.getAttribute("href")).toBe("#schedule");
    expect(link.textContent).toContain(`${en["settings.section.schedule"]}:`);
  });

  it("each warning needs its tick", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(
      api,
      dryRunOut({
        warnings: [
          { code: "paid_refresh_slow", detail: { cadence_days: 9.5 } },
          { code: "adzuna_rotation", detail: { cadence_days: null } },
        ],
        effects: ["schedule"],
      }),
    );
    renderSettings(["scope", "schedule"]);
    await change();
    const slow = await waitFor(() => warningIn("schedule", "warning.paid_refresh_slow"));
    const rotation = warningIn("schedule", "warning.adzuna_rotation");
    expect(slow.textContent).toContain("9.5");
    expect(rotation.textContent).toContain(en["warning_detail.adzuna_rotation_never"]);
    expect(saveIn("schedule").disabled).toBe(true);
    await userEvent.setup().click(within(slow).getByRole("checkbox", { name: en["settings_form.understand"] }));
    expect(saveIn("schedule").disabled).toBe(true);
    // ticked in one panel, ticked in every panel
    const inScope = within(warningIn("scope", "warning.paid_refresh_slow")).getByRole<HTMLInputElement>("checkbox");
    expect(inScope.checked).toBe(true);
    await userEvent.setup().click(within(warningIn("scope", "warning.adzuna_rotation")).getByRole("checkbox"));
    expect(saveIn("schedule").disabled).toBe(false);
    expect(saveIn("scope").disabled).toBe(false);
  });

  it("effect chips and the re-score cost", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(
      api,
      dryRunOut({
        warnings: [{ code: "llm_rescore", detail: { targets: 1180, est_usd: "41.300000", bulk_budget_usd: "22.440000" } }],
        effects: ["llm_tuple", "none", "scope"],
      }),
    );
    renderSettings(["schedule"]);
    await change();
    const chips = await within(section("schedule")).findByRole("list", { name: en["settings_form.effects"] });
    const texts = within(chips).getAllByRole("listitem").map((chip) => chip.textContent);
    expect(texts).toEqual(["LLM re-score ≈ $41.30", en["effect_class.scope"]]);
    expect(within(section("schedule")).getByText(en["settings_form.cost_note"])).toBeTruthy();
    expect(warningIn("schedule", "warning.llm_rescore").textContent).toContain("1,180");
  });

  it("the LLM re-score chip without its warning has no cost", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, dryRunOut({ effects: ["llm_tuple"] }));
    renderSettings(["schedule"]);
    await change();
    const chips = await within(section("schedule")).findByRole("list", { name: en["settings_form.effects"] });
    expect(within(chips).getAllByRole("listitem").map((chip) => chip.textContent)).toEqual([en["effect_class.llm_tuple"]]);
  });

  it("a standing warning is compact", async () => {
    const api = fakeApi({ signedIn: true });
    const binding = { code: "llm_cap_binding", detail: { expected_scored_per_month: 1700 } };
    dryRuns(
      api,
      dryRunOut({ warnings: [binding, { code: "paid_refresh_slow", detail: { cadence_days: 9.5 } }], effects: ["schedule"] }),
      dryRunOut({ warnings: [binding] }),
    );
    renderSettings(["schedule"]);
    await change();
    const full = await waitFor(() => warningIn("schedule", "warning.paid_refresh_slow"));
    expect(within(full).getByRole("checkbox", { name: en["settings_form.understand"] })).toBeTruthy();
    const standing = warningIn("schedule", "warning.llm_cap_binding");
    expect(within(standing).queryByText(en["settings_form.understand"])).toBeNull();
    expect(within(standing).queryByText(/1,700/)).toBeNull(); // one line: no detail
    expect(standing.textContent).toContain(en["settings_form.standing"]);
    const tick = within(standing).getByRole("checkbox", { name: new RegExp(en["warning.llm_cap_binding"]) });
    await userEvent.setup().click(within(full).getByRole("checkbox"));
    expect(saveIn("schedule").disabled).toBe(true); // a standing warning needs its tick too
    await userEvent.setup().click(tick);
    expect(saveIn("schedule").disabled).toBe(false);
  });

  it("a standing warning comes from the base dry-run", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, dryRunOut(), dryRunOut({ warnings: [{ code: "llm_cap_binding", detail: { expected_scored_per_month: 1700 } }] }));
    renderSettings(["schedule"]);
    expect(await within(section("schedule")).findByText(en["settings_form.no_changes"])).toBeTruthy();
    const standing = await waitFor(() => warningIn("schedule", "warning.llm_cap_binding"));
    expect(standing.textContent).toContain(en["settings_form.standing"]);
    expect(dryRunPayloads(api)).toEqual([settingsRead().payload]); // the base document, once
    expect(saveIn("schedule").disabled).toBe(true); // nothing to save
  });

  it("shows the projections as compact rows", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, dryRunOut({ effects: ["schedule"] }));
    renderSettings(["schedule"]);
    await change();
    const rows = await within(section("schedule")).findByRole("list", { name: en["settings_form.preview"] });
    const jsearch = within(rows).getByText(en["provider.jsearch"]).closest("li");
    expect(jsearch?.textContent).toContain("112 of 200");
    const llm = within(rows).getByText(en["dry_run.llm"]).closest("li");
    expect(llm?.textContent).toContain("$92.40");
    expect(llm?.textContent).toContain("1,700");
  });

  it("a warning code without a text shows the code", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, dryRunOut({ warnings: [{ code: "brand_new_warning", detail: {} }], effects: ["schedule"] }));
    renderSettings(["schedule"]);
    await change();
    const code = await within(section("schedule")).findByText("brand_new_warning");
    expect(code.tagName).toBe("CODE");
  });
});
