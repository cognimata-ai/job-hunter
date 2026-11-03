import { describe, expect, it } from "vitest";
import { toSearchParams } from "./query";

describe("toSearchParams", () => {
  it("writes scalars as text, arrays as repeated keys, and leaves null and undefined out", () => {
    const params = toSearchParams({
      q: "backend berlin",
      limit: 50,
      remote: true,
      seen: false,
      source: ["adzuna", "lever"],
      cursor: null,
      sort: undefined,
      empty: [],
    });
    expect([...params]).toEqual([
      ["q", "backend berlin"],
      ["limit", "50"],
      ["remote", "true"],
      ["seen", "false"],
      ["source", "adzuna"],
      ["source", "lever"],
    ]);
    expect(params.toString()).toBe("q=backend+berlin&limit=50&remote=true&seen=false&source=adzuna&source=lever");
  });

  it("keeps an empty string and a zero, which are values", () => {
    expect([...toSearchParams({ q: "", min: 0 })]).toEqual([["q", ""], ["min", "0"]]);
  });
});
