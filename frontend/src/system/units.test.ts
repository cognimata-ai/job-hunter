import { describe, expect, it } from "vitest";
import { makeUnits } from "./units";

describe("makeUnits", () => {
  it("formats a duration in its two largest units, dropping a zero second unit", () => {
    const en = makeUnits("en-GB");
    expect(en.duration(0)).toBe("0 secs");
    expect(en.duration(45)).toBe("45 secs");
    expect(en.duration(125)).toBe("2 mins 5 secs");
    expect(en.duration(14_232)).toBe("3 hrs 57 mins");
    expect(en.duration(7_200)).toBe("2 hrs");
    expect(en.duration(90_061)).toBe("1 day 1 hr");
    expect(makeUnits("de-DE").duration(14_232)).toBe("3 Std. 57 Min.");
  });

  it("formats a size in the largest decimal unit below 1,000 of it", () => {
    const en = makeUnits("en-GB");
    expect(en.bytes(512)).toBe("512 byte");
    expect(en.bytes(8_192)).toBe("8.2 kB");
    expect(en.bytes(58_742_451)).toBe("58.7 MB");
    expect(en.bytes(270_553_174_016)).toBe("270.6 GB");
    expect(makeUnits("de-DE").bytes(58_742_451)).toBe("58,7 MB");
  });
});
