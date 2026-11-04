import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { fakeApi, json, place, problem, requestsTo } from "../test/api";
import { renderInProviders } from "../test/app";
import { PlacePicker } from "./PlacePicker";

afterEach(() => {
  vi.restoreAllMocks();
});

function picker() {
  const onChange = vi.fn();
  renderInProviders(<PlacePicker label={en["filters.place"]} value={null} onChange={onChange} />);
  const input = screen.getByRole("combobox", { name: en["filters.place"] });
  const popup = document.getElementById(input.getAttribute("aria-controls") ?? "")?.parentElement;
  if (popup == null) {
    throw new Error("no popup");
  }
  return { input, popup, onChange };
}

describe("PlacePicker", () => {
  it("says it is searching while the places are read, and lists them once read", async () => {
    const api = fakeApi({ signedIn: true });
    let answer: (() => void) | null = null;
    api.routes.set("GET /api/geo/places", () =>
      new Promise<Response>((resolve) => {
        answer = () => { resolve(json([place()])); };
      }),
    );
    const { input, popup } = picker();
    await userEvent.setup().type(input, "Ber");
    expect(input.getAttribute("aria-expanded")).toBe("true");
    expect(within(popup).getByText(en["combobox.searching"])).toBeTruthy(); // while typing, then reading
    expect(within(popup).queryByText(en["filters.place_none"])).toBeNull();
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/geo/places")).toHaveLength(1); // the search, 250 ms after the typing
    });
    expect(within(popup).getByText(en["combobox.searching"])).toBeTruthy(); // and while it is read
    await act(async () => {
      answer?.();
      await Promise.resolve();
    });
    expect(await within(popup).findByRole("option", { name: /^Berlin/ })).toBeTruthy();
    expect(within(popup).queryByText(en["combobox.searching"])).toBeNull();
  });

  it("says the places could not be read when the read fails, not that none was found", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/geo/places", () => problem(422, "validation_failed"));
    const { input, popup } = picker();
    await userEvent.setup().type(input, "Ber");
    expect(await within(popup).findByText(en["filters.place_failed"])).toBeTruthy();
    expect(within(popup).queryByText(en["filters.place_none"])).toBeNull();
    expect(within(popup).queryByText(en["combobox.searching"])).toBeNull();
    expect(within(popup).queryAllByRole("option")).toHaveLength(0);
  });

  it("says none was found for a search with no place", async () => {
    const api = fakeApi({ signedIn: true });
    api.places = [];
    const { input, popup } = picker();
    await userEvent.setup().type(input, "Xyz");
    expect(await within(popup).findByText(en["filters.place_none"])).toBeTruthy();
  });
});
