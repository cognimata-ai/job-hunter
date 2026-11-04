import { describe, expect, it } from "vitest";
import { mergeSpans, sliceByCodePoints, splitBySpans } from "./codepoints";

describe("sliceByCodePoints", () => {
  it("slices after an emoji by code points", () => {
    // "🚀" is one code point but two UTF-16 units: `String.prototype.slice` would cut "Pytho"
    expect("🚀 Python".slice(2, 8)).toBe(" Pytho");
    expect(sliceByCodePoints("🚀 Python", 2, 8)).toBe("Python");
    expect(sliceByCodePoints("Grüße 👋🏽 aus Köln", 9, 12)).toBe("aus");
  });
});

describe("mergeSpans", () => {
  it("drops empty and out-of-range spans, then merges overlapping and touching ones in order", () => {
    expect(
      mergeSpans(
        [
          { start: 8, end: 10 },
          { start: 0, end: 3 },
          { start: 2, end: 5 },
          { start: 5, end: 6 },
          { start: 7, end: 7 },
          { start: -1, end: 2 },
          { start: 9, end: 13 },
          { start: 4, end: 2 },
        ],
        12,
      ),
    ).toEqual([
      { start: 0, end: 6 },
      { start: 8, end: 10 },
    ]);
  });
});

describe("splitBySpans", () => {
  it("cuts the text into marked and unmarked pieces by code points", () => {
    expect(splitBySpans("🚀 Python and Go", [{ start: 2, end: 8 }, { start: 13, end: 15 }])).toEqual([
      { text: "🚀 ", marked: false },
      { text: "Python", marked: true },
      { text: " and ", marked: false },
      { text: "Go", marked: true },
    ]);
  });

  it("is one unmarked piece without a span, and no piece for an empty text", () => {
    expect(splitBySpans("Python", [])).toEqual([{ text: "Python", marked: false }]);
    expect(splitBySpans("", [{ start: 0, end: 1 }])).toEqual([]);
  });
});
