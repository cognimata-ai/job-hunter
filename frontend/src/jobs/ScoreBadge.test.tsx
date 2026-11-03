import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { components } from "../api/schema";
import i18n from "../i18n/index";
import { ScoreBadge } from "./ScoreBadge";

type ScoreOut = components["schemas"]["ScoreOut"];

const SCORE: ScoreOut = {
  mid: "82.40",
  low: "74.00",
  high: "89.50",
  coverage: "0.850",
  confidence: "high",
  result_status: "accepted",
};

afterEach(async () => {
  await i18n.changeLanguage("en");
});

describe("ScoreBadge", () => {
  it("82, 74–90, cov 85%, high", () => {
    const { container } = render(<ScoreBadge score={SCORE} />);
    expect(screen.getByText("82")).toBeTruthy();
    expect(screen.getByText("74–90")).toBeTruthy();
    expect(screen.getByText("cov 85%")).toBeTruthy();
    expect(screen.getByText("high")).toBeTruthy();
    // the confidence is named, not only coloured
    expect(container.textContent).toContain("confidence high");
  });

  it("the score bar has a text label", () => {
    const { container } = render(<ScoreBadge score={SCORE} />);
    const bar = screen.getByRole("img", { name: "Score 82, range 74 to 90" });
    // the interval is drawn by SVG attributes, never by a style
    const rects = [...bar.querySelectorAll("rect")];
    const interval = rects.find((rect) => rect.getAttribute("data-part") === "interval");
    expect(interval?.getAttribute("x")).toBe("74");
    expect(interval?.getAttribute("width")).toBe("16");
    expect(container.querySelectorAll("[style]")).toHaveLength(0);
  });

  it("German texts", async () => {
    await i18n.changeLanguage("de");
    render(<ScoreBadge score={{ ...SCORE, coverage: "0.285", confidence: "medium" }} />);
    expect(screen.getByRole("img", { name: "Bewertung 82, Spanne 74 bis 90" })).toBeTruthy();
    expect(screen.getByText("Abd. 29 %")).toBeTruthy();
    expect(screen.getByText("mittel")).toBeTruthy();
  });
});
