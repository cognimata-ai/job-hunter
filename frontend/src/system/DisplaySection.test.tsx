import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import de from "../i18n/de.json";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import { fakeApi, json, requestsTo, settingsRead } from "../test/api";
import { renderApp } from "../test/app";
import { dryRunPayloads, saveBody, saves } from "../test/settings";

type SettingsOut = components["schemas"]["SettingsOut"];

afterEach(async () => {
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

function display(): HTMLElement {
  const element = document.getElementById("display");
  if (element === null) {
    throw new Error("no display part");
  }
  return element;
}

describe("DisplaySection", () => {
  it("sits first in System, framed with its own panel, the operational parts after it", async () => {
    fakeApi({ signedIn: true });
    renderApp("/settings#system");
    await screen.findByRole("region", { name: en["settings_display.title"] });
    await within(display()).findByRole("combobox", { name: en["settings_display.language"] });
    const system = document.getElementById("system");
    expect(system?.tagName).toBe("SECTION");
    expect(system?.contains(display())).toBe(true);
    expect(display().tagName).toBe("SECTION");
    expect(within(display()).getByRole("heading", { level: 3 }).textContent).toBe(en["settings_display.title"]);
    expect(within(display()).getByRole("button", { name: en["settings_form.save"] })).toBeTruthy();
    // the parts of System in order: Display first, then the operational ones, which have no panel
    const parts = [...(system?.querySelectorAll(":scope h3") ?? [])].map((h) => h.textContent);
    expect(parts).toEqual([
      en["settings_display.title"],
      en["attention.title"],
      en["settings_system.runs"],
      en["settings_system.cycles"],
      en["settings_system.alerts"],
      en["settings_system.golden"],
      en["settings_system.health"],
    ]);
    expect(within(system as HTMLElement).getAllByRole("button", { name: en["settings_form.save"] })).toHaveLength(1);
  });

  it("offers the time zones, the current one even when the list lacks it, the languages and the threshold", async () => {
    const api = fakeApi({ signedIn: true });
    api.timezone = "UTC"; // valid, but not in `Intl.supportedValuesOf("timeZone")`
    renderApp("/settings#display");
    await screen.findByRole("region", { name: en["settings_display.title"] });
    const zone = await within(display()).findByRole<HTMLSelectElement>("combobox", { name: en["settings_display.timezone"] });
    expect(zone.value).toBe("UTC");
    const zones = [...zone.options].map((option) => option.value);
    expect(zones).toContain("Europe/Berlin");
    expect(zones).toContain("UTC");
    expect(zones.length).toBe(Intl.supportedValuesOf("timeZone").length + 1);

    const language = within(display()).getByRole<HTMLSelectElement>("combobox", { name: en["settings_display.language"] });
    expect([...language.options].map((option) => [option.value, option.textContent])).toEqual([
      ["en", "English"],
      ["de", "Deutsch"],
    ]);
    const threshold = within(display()).getByRole<HTMLSelectElement>("combobox", { name: en["settings_display.threshold"] });
    expect([...threshold.options].map((option) => [option.value, option.textContent])).toEqual([
      ["warn", en["alert_severity.warn"]],
      ["critical", en["alert_severity.critical"]],
    ]);
    fireEvent.change(threshold, { target: { value: "critical" } });
    fireEvent.change(zone, { target: { value: "Europe/Lisbon" } });
    await waitFor(() => {
      const last = dryRunPayloads(api).at(-1);
      expect(last?.notifications.push_min_severity).toBe("critical");
      expect(last?.display.timezone).toBe("Europe/Lisbon");
    });
  });

  it("a display save switches the language", async () => {
    const api = fakeApi({ signedIn: true });
    let current: SettingsOut = settingsRead();
    api.routes.set("GET /api/settings", () => json(current));
    api.routes.set("POST /api/settings", (request) => {
      const { payload } = saveBody(request);
      current = { ...current, version: { ...current.version, id: 42, version_no: 13 }, payload };
      return json({ version: { id: 42, version_no: 13, effect_classes: [] }, apply_scope_operation_id: null }, 201);
    });
    renderApp("/settings#display");
    await screen.findByRole("region", { name: en["settings_display.title"] });
    const language = await within(display()).findByRole("combobox", { name: en["settings_display.language"] });
    fireEvent.change(language, { target: { value: "de" } });
    const save = within(display()).getByRole("button", { name: en["settings_form.save"] });
    await waitFor(() => {
      expect((save as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(save);
    await waitFor(() => {
      expect(saves(api)).toHaveLength(1);
    });
    expect(saveBody(saves(api)[0]).payload.display.ui_language).toBe("de");
    // the shell's read of the settings switches the UI
    expect(await screen.findByRole("heading", { level: 1, name: de["settings.title"] })).toBeTruthy();
    expect(within(display()).getByRole("heading", { level: 3 }).textContent).toBe(de["settings_display.title"]);
    expect(document.documentElement.lang).toBe("de");
    expect(requestsTo(api, "GET /api/settings").length).toBeGreaterThan(1);
  });
});
