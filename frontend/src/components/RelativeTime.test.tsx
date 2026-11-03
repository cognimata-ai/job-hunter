import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import i18n from "../i18n/index";
import { RelativeTime } from "./RelativeTime";

const NOW = new Date("2026-11-03T12:00:00Z");

afterEach(async () => {
  await i18n.changeLanguage("en");
});

describe("RelativeTime", () => {
  it("says how long ago in the UI language, with the instant and the absolute time in the time zone", async () => {
    const { rerender } = render(<RelativeTime value="2026-11-03T10:00:00Z" tz="Europe/Berlin" now={NOW} />);
    const time = screen.getByText("2 hours ago");
    expect(time.tagName).toBe("TIME");
    expect(time.getAttribute("datetime")).toBe("2026-11-03T10:00:00.000Z");
    expect(time.getAttribute("title")).toContain("11:00"); // 10:00 UTC is 11:00 in Berlin in November
    await i18n.changeLanguage("de");
    rerender(<RelativeTime value="2026-11-03T10:00:00Z" tz="Europe/Berlin" now={NOW} />);
    expect(screen.getByText("vor 2 Stunden")).toBeTruthy();
  });

  it("says now for the last seconds, and the coming time for a future instant", () => {
    render(
      <>
        <RelativeTime value="2026-11-03T11:59:40Z" tz="UTC" now={NOW} />
        <RelativeTime value="2026-11-05T12:00:00Z" tz="UTC" now={NOW} />
      </>,
    );
    expect(screen.getByText("now")).toBeTruthy();
    expect(screen.getByText("in 2 days")).toBeTruthy();
  });

  it("renders nothing for a value that is no time", () => {
    const { container } = render(<RelativeTime value="not a time" tz="UTC" now={NOW} />);
    expect(container.innerHTML).toBe("");
  });
});
