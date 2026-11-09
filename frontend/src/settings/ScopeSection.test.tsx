import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import codes from "../i18n/codes.json";
import en from "../i18n/en.json";
import { dryRunOut, fakeApi, json, place, projectionsOut, settingsRead } from "../test/api";
import { dryRunPayloads, renderSettings } from "../test/settings";
import { POLICY_LANGUAGES } from "./ScopeSection";

afterEach(() => {
  vi.restoreAllMocks();
});

function scope(): HTMLElement {
  const element = document.getElementById("scope");
  if (element === null) {
    throw new Error("no scope section");
  }
  return element;
}

describe("ScopeSection", () => {
  it("adds a place with postal codes and population and a radius", async () => {
    const api = fakeApi({ signedIn: true });
    api.places = [place()];
    renderSettings(["scope"]);
    const user = userEvent.setup();
    await user.click(await within(scope()).findByRole("button", { name: en["settings_scope.add_place"] }));
    await user.type(within(scope()).getByRole("combobox", { name: en["settings_scope.place_to_add"] }), "Ber");
    await user.click(await within(scope()).findByRole("option", { name: /^Berlin/ }));

    const list = within(scope()).getByRole("list", { name: en["settings_scope.locations"] });
    const [row] = within(list).getAllByRole("listitem");
    expect(row.textContent).toContain("Berlin");
    expect(row.textContent).toContain("10115, 10117, 10119 +2");
    expect(row.textContent).toContain("3,426,354");
    const radius = within(row).getByRole("slider", { name: en["settings_scope.radius"] });
    expect(radius.getAttribute("aria-valuenow")).toBe("30");
    expect(radius.getAttribute("aria-valuetext")).toBe("30 km");
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.scope.locations).toEqual([
        { label: "Berlin", gazetteer_id: "geonames:2950159", lat: 52.52437, lon: 13.41053, radius_km: 30 },
      ]);
    });
    radius.focus();
    await user.keyboard("{ArrowRight}");
    expect(radius.getAttribute("aria-valuenow")).toBe("31");
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.scope.locations[0]?.radius_km).toBe(31);
    });
    // removed again, the scope is the saved one
    await user.click(within(row).getByRole("button", { name: "Remove Berlin" }));
    expect(within(list).queryAllByRole("listitem")).toHaveLength(0);
    expect(await within(scope()).findByText(en["settings_form.no_changes"])).toBeTruthy();
  });

  it("shows a saved place's postal codes and population from the gazetteer", async () => {
    const api = fakeApi({ signedIn: true });
    api.places = [place({ gazetteer_id: "geonames:1", label: "Berlin", postal_codes: ["99999"] }), place()];
    const base = settingsRead();
    api.routes.set("GET /api/settings", () =>
      json({
        ...base,
        payload: {
          ...base.payload,
          scope: {
            ...base.payload.scope,
            locations: [{ label: "Berlin", gazetteer_id: "geonames:2950159", lat: 52.52437, lon: 13.41053, radius_km: 50 }],
          },
        },
      }),
    );
    renderSettings(["scope"]);
    const list = await within(scope()).findByRole("list", { name: en["settings_scope.locations"] });
    await waitFor(() => {
      expect(within(list).getByRole("listitem").textContent).toContain("10115, 10117, 10119 +2");
    });
    expect(within(list).getByRole("slider", { name: en["settings_scope.radius"] }).getAttribute("aria-valuenow")).toBe("50");
  });

  it("the recompute line", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/settings/dry-run", (request) => {
      const isBase = request.body === JSON.stringify({ payload: settingsRead().payload });
      return json(isBase ? dryRunOut() : dryRunOut({ effects: ["scope"], projections: projectionsOut({ det_recompute: 1180 }) }));
    });
    renderSettings(["scope"]);
    expect(within(scope()).queryByText(/No LLM cost/)).toBeNull();
    await userEvent.setup().click(await within(scope()).findByRole("checkbox", { name: en["settings_scope.remote_within_germany"] }));
    expect(await within(scope()).findByText("No LLM cost — 1,180 scores will be recomputed")).toBeTruthy();
  });

  it("the eleventh place cannot be added", async () => {
    const api = fakeApi({ signedIn: true });
    const base = settingsRead();
    const locations = Array.from({ length: 10 }, (_, i) => ({
      label: `Ort ${String(i + 1)}`,
      gazetteer_id: `geonames:${String(i + 1)}`,
      lat: 50 + i / 10,
      lon: 10,
      radius_km: 30,
    }));
    api.routes.set("GET /api/settings", () => json({ ...base, payload: { ...base.payload, scope: { ...base.payload.scope, locations } } }));
    renderSettings(["scope"]);
    const add = (await within(scope()).findByRole<HTMLButtonElement>("button", { name: en["settings_scope.add_place"] }));
    expect(add.disabled).toBe(true);
    const reason = document.getElementById(add.getAttribute("aria-describedby") ?? "");
    expect(reason?.textContent).toBe("At most 10 places — remove one to add another");
    await userEvent.setup().click(within(scope()).getByRole("button", { name: "Remove Ort 3" }));
    expect((within(scope()).getByRole<HTMLButtonElement>("button", { name: en["settings_scope.add_place"] })).disabled).toBe(false);
  });

  it("moves focus to the new row when the tenth place fills the list", async () => {
    const api = fakeApi({ signedIn: true });
    api.places = [place()];
    const base = settingsRead();
    const locations = Array.from({ length: 9 }, (_, i) => ({
      label: `Ort ${String(i + 1)}`,
      gazetteer_id: `geonames:${String(i + 1)}`,
      lat: 50 + i / 10,
      lon: 10,
      radius_km: 30,
    }));
    api.routes.set("GET /api/settings", () => json({ ...base, payload: { ...base.payload, scope: { ...base.payload.scope, locations } } }));
    renderSettings(["scope"]);
    const user = userEvent.setup();
    await user.click(await within(scope()).findByRole("button", { name: en["settings_scope.add_place"] }));
    await user.type(within(scope()).getByRole("combobox", { name: en["settings_scope.place_to_add"] }), "Ber");
    await user.click(await within(scope()).findByRole("option", { name: /^Berlin/ }));
    const rows = within(within(scope()).getByRole("list", { name: en["settings_scope.locations"] })).getAllByRole("listitem");
    expect(rows).toHaveLength(10);
    expect(within(scope()).getByRole<HTMLButtonElement>("button", { name: en["settings_scope.add_place"] }).disabled).toBe(true);
    // not left on a disabled Add (the body): on the new row's first control
    expect(document.activeElement).toBe(within(rows[9]).getByRole("button", { name: "Remove Berlin" }));
  });

  it("keeps one work mode ticked and writes the set in its order", async () => {
    const api = fakeApi({ signedIn: true });
    renderSettings(["scope"]);
    const group = await within(scope()).findByRole("group", { name: en["settings_scope.work_modes"] });
    const user = userEvent.setup();
    await user.click(within(group).getByRole("checkbox", { name: en["filters.work_mode.remote"] }));
    await user.click(within(group).getByRole("checkbox", { name: en["filters.work_mode.hybrid"] }));
    const onsite = within(group).getByRole<HTMLInputElement>("checkbox", { name: en["filters.work_mode.onsite"] });
    expect(onsite.disabled).toBe(true);
    expect(within(group).getByText(en["filters.last_one"])).toBeTruthy();
    await user.click(within(group).getByRole("checkbox", { name: en["filters.work_mode.remote"] }));
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.scope.work_modes).toEqual(["remote", "onsite"]);
    });
  });

  it("a language left at the default is no key of the policy", async () => {
    const api = fakeApi({ signedIn: true });
    renderSettings(["scope"]);
    const french = await within(scope()).findByRole("combobox", { name: en["language_code.fr"] });
    const user = userEvent.setup();
    await user.selectOptions(french, "soft");
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.scope.language_policy).toEqual({ fr: "soft" });
    });
    await user.selectOptions(french, "");
    expect(await within(scope()).findByText(en["settings_form.no_changes"])).toBeTruthy();
  });

  it("lists the languages of the catalogue in the language requirements", () => {
    expect([...POLICY_LANGUAGES].sort()).toEqual(codes.language_code);
  });

  it("adds and removes an excluded keyword", async () => {
    const api = fakeApi({ signedIn: true });
    renderSettings(["scope"]);
    const field = await within(scope()).findByRole("textbox", { name: en["settings_scope.excluded_keywords"] });
    const user = userEvent.setup();
    await user.type(field, "Praktikum{Enter}");
    await user.type(field, "Werkstudent{Enter}");
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.scope.excluded_keywords).toEqual(["Praktikum", "Werkstudent"]);
    });
    await user.click(within(scope()).getByRole("button", { name: "Remove Praktikum" }));
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.scope.excluded_keywords).toEqual(["Werkstudent"]);
    });
    expect(screen.queryByText("Praktikum")).toBeNull();
  });
});
