import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { makeFormat } from "../system/format";
import { dryRunOut, fakeApi, json, projectionsOut, requestsTo, settingsRead } from "../test/api";
import { dryRunPayloads, renderSettings } from "../test/settings";

afterEach(() => {
  vi.restoreAllMocks();
});

function schedule(): HTMLElement {
  const element = document.getElementById("schedule");
  if (element === null) {
    throw new Error("no schedule section");
  }
  return element;
}

/** The list that follows the heading `name` in the section. */
function listAfter(name: string): string[] {
  const heading = within(schedule()).getByText(name);
  const list = heading.nextElementSibling;
  if (list === null) {
    throw new Error(`no list after ${name}`);
  }
  return within(list as HTMLElement).getAllByRole("listitem").map((item) => item.textContent);
}

describe("ScheduleSection", () => {
  it("next slots of the saved and the draft schedule", async () => {
    const api = fakeApi({ signedIn: true });
    const berlin = makeFormat("en-GB", "Europe/Berlin");
    api.routes.set("POST /api/settings/dry-run", (request) => {
      const isBase = request.body === JSON.stringify({ payload: settingsRead().payload });
      return json(isBase ? dryRunOut() : dryRunOut({ effects: ["schedule"], projections: projectionsOut({ next_slot: "2026-11-03T15:00:00Z" }) }));
    });
    renderSettings(["schedule"]);
    await waitFor(() => {
      expect(listAfter(en["settings_schedule.next_saved"])).toEqual(api.status.next_slots.map((slot) => berlin.dayMonthTime(slot)));
    });
    expect(listAfter(en["settings_schedule.next_saved"])).toEqual(["3 Nov, 14:00", "3 Nov, 20:00", "4 Nov, 02:00"]);
    expect(within(schedule()).queryByText(en["settings_schedule.next_draft"])).toBeNull();

    fireEvent.change(screen.getByRole("spinbutton", { name: en["settings_schedule.interval"] }), { target: { value: "8" } });
    const draft = await within(schedule()).findByText(en["settings_schedule.next_draft"]);
    expect(draft.nextElementSibling?.textContent).toBe("3 Nov, 16:00");
    expect(requestsTo(api, "GET /api/system/status").length).toBeGreaterThanOrEqual(1);
  });

  it("says when the draft schedule has no next run", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/settings/dry-run", (request) => {
      const isBase = request.body === JSON.stringify({ payload: settingsRead().payload });
      return json(isBase ? dryRunOut() : dryRunOut({ effects: ["schedule"], projections: projectionsOut({ next_slot: null }) }));
    });
    renderSettings(["schedule"]);
    fireEvent.click(await within(schedule()).findByRole("checkbox", { name: en["settings_schedule.enabled"] }));
    const draft = await within(schedule()).findByText(en["settings_schedule.next_draft"]);
    expect(draft.nextElementSibling?.textContent).toBe(en["settings_schedule.next_none"]);
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.schedule.enabled).toBe(false);
    });
  });

  it("edits the anchor time in the display time zone", async () => {
    const api = fakeApi({ signedIn: true });
    renderSettings(["schedule"]);
    const anchor = await within(schedule()).findByLabelText(en["settings_schedule.anchor"]);
    expect((anchor as HTMLInputElement).value).toBe("06:00");
    // the interval's range is the brief's (1–168 h), the only bound the section writes out
    const interval = within(schedule()).getByRole("spinbutton", { name: en["settings_schedule.interval"] });
    expect([interval.getAttribute("min"), interval.getAttribute("max")]).toEqual(["1", "168"]);
    expect(within(schedule()).getByText("Local time in Europe/Berlin")).toBeTruthy();
    fireEvent.change(anchor, { target: { value: "07:30" } });
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.schedule.anchor_local).toBe("07:30");
    });
  });
});
