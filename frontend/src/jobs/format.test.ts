import { afterEach, describe, expect, it } from "vitest";
import de from "../i18n/de.json";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import { NOW } from "../test/app";
import { percentInt, postedLabel, scoreInt, seenToday } from "./format";

const BERLIN = "Europe/Berlin";

afterEach(async () => {
  await i18n.changeLanguage("en");
});

describe("postedLabel", () => {
  // 22:30 UTC on 2 Nov is 23:30 in Berlin: the calendar day before NOW (08:00 on 3 Nov in Berlin), 8.5 h ago
  const LATE_YESTERDAY = "2026-11-02T22:30:00Z";

  it("approximate for day, relative_day and ingest", () => {
    const t = i18n.t;
    expect(postedLabel(LATE_YESTERDAY, "day", NOW, BERLIN, t)).toBe("≈ yesterday");
    expect(postedLabel(LATE_YESTERDAY, "relative_day", NOW, BERLIN, t)).toBe("≈ yesterday");
    expect(postedLabel(LATE_YESTERDAY, "ingest", NOW, BERLIN, t)).toBe("≈ yesterday (ingest date)");
    // an exact or hourly date is elapsed time in the largest fitting unit, with no ≈
    expect(postedLabel(LATE_YESTERDAY, "exact", NOW, BERLIN, t)).toBe("8 hours ago");
    expect(postedLabel(LATE_YESTERDAY, "hour", NOW, BERLIN, t)).toBe("8 hours ago");
    expect(postedLabel("2026-10-31T23:30:00Z", "day", NOW, BERLIN, t)).toBe("≈ 2 days ago");
  });

  it("counts an approximate date in calendar days of the time zone", () => {
    // 16:00 UTC on 2 Nov: 17:00 on 2 Nov in Berlin, but 01:00 on 3 Nov in Tokyo, where NOW is 16:00 on 3 Nov
    expect(postedLabel("2026-11-02T16:00:00Z", "day", NOW, BERLIN, i18n.t)).toBe("≈ yesterday");
    expect(postedLabel("2026-11-02T16:00:00Z", "day", NOW, "Asia/Tokyo", i18n.t)).toBe("≈ today");
  });

  it("ingest date label", async () => {
    expect(postedLabel("2026-10-30T12:00:00Z", "ingest", NOW, BERLIN, i18n.t)).toBe("≈ 4 days ago (ingest date)");
    expect(postedLabel(null, "unknown", NOW, BERLIN, i18n.t)).toBe(en["posted.unknown"]);
    await i18n.changeLanguage("de");
    expect(postedLabel("2026-10-30T12:00:00Z", "ingest", NOW, BERLIN, i18n.t)).toBe("≈ vor 4 Tagen (Erfassungsdatum)");
    expect(postedLabel(LATE_YESTERDAY, "exact", NOW, BERLIN, i18n.t)).toBe("vor 8 Stunden");
    expect(postedLabel(null, "unknown", NOW, BERLIN, i18n.t)).toBe(de["posted.unknown"]);
  });
});

describe("scoreInt", () => {
  it("half-up integers", () => {
    expect(scoreInt("82.5")).toBe(83);
    expect(scoreInt("82.4999999")).toBe(82);
    expect(scoreInt("0")).toBe(0);
    expect(scoreInt("74.00")).toBe(74);
    expect(scoreInt("89.60")).toBe(90);
    expect(scoreInt("99.5")).toBe(100);
    expect(scoreInt("100.000000")).toBe(100);
    expect(scoreInt("7.")).toBe(7);
  });

  it("scoreInt rounds half-up on the string", () => {
    // as a float this is 82.5 exactly, which Math.round takes up; the decimal is below one half
    const below = "82.49999999999999999";
    expect(Math.round(Number(below))).toBe(83);
    expect(scoreInt(below)).toBe(82);
    expect(scoreInt("1.4999999999999999")).toBe(1);
  });

  it("refuses what is no decimal string", () => {
    expect(() => scoreInt("8.25E+1")).toThrow();
    expect(() => scoreInt("")).toThrow();
    expect(() => scoreInt("-3")).toThrow();
  });
});

describe("percentInt", () => {
  it("percentInt of a coverage fraction", () => {
    expect(percentInt("0.85")).toBe(85);
    expect(percentInt("0.850")).toBe(85);
    expect(percentInt("0.8")).toBe(80);
    expect(percentInt("1")).toBe(100);
    expect(percentInt("1.000")).toBe(100);
    expect(percentInt("0")).toBe(0);
    expect(percentInt("0.005")).toBe(1);
    expect(percentInt("0.0049")).toBe(0);
    expect(percentInt("0.999")).toBe(100);
    // Number(x) * 100 is 28.499999999999996 and 57.49999999999999 here, which round down
    expect(Math.round(Number("0.285") * 100)).toBe(28);
    expect(percentInt("0.285")).toBe(29);
    expect(percentInt("0.575")).toBe(58);
  });
});

describe("seenToday", () => {
  it("compares the calendar dates in the time zone", () => {
    // 23:30 UTC on 2 Nov is 00:30 on 3 Nov in Berlin, NOW's day there; in UTC it is the day before
    expect(seenToday("2026-11-02T23:30:00Z", NOW, BERLIN)).toBe(true);
    expect(seenToday("2026-11-02T23:30:00Z", NOW, "UTC")).toBe(false);
    expect(seenToday("2026-11-02T22:30:00Z", NOW, BERLIN)).toBe(false);
    expect(seenToday("2026-11-03T06:59:00Z", NOW, "UTC")).toBe(true);
  });
});
