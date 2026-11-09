import { describe, expect, it } from "vitest";
import { reviewDraft, sentEdits } from "../../test/fixtures/drafts";
import {
  editSetOf,
  MAX_SPAN_EDITS,
  type SegmentOut,
  toCodePoints,
  toRawRange,
  withRemoval,
  withRestore,
} from "./segments";

function text(stripped: [number, number], raw: [number, number]): SegmentOut {
  return {
    kind: "text",
    category: null,
    replacement_index: null,
    restorable: false,
    stripped_start: stripped[0],
    stripped_end: stripped[1],
    raw_start: raw[0],
    raw_end: raw[1],
  };
}

function replacement(
  category: NonNullable<SegmentOut["category"]>,
  index: number | null,
  stripped: [number, number],
  raw: [number, number],
): SegmentOut {
  return { ...text(stripped, raw), kind: "replacement", category, replacement_index: index, restorable: true };
}

function removed(stripped: [number, number], raw: [number, number]): SegmentOut {
  return { ...text(stripped, raw), kind: "removed" };
}

// The brief's example: raw `Max Mustermann, max@example.org, +49 170 1234567` stripped to
// `[NAME], [EMAIL], +49 170 1234567` (a map written out by hand: the name is replaced here, which the real
// rules do not do inside a line).
const BRIEF_MAP: SegmentOut[] = [
  replacement("name", 0, [0, 6], [0, 14]),
  text([6, 8], [14, 16]),
  replacement("email", 1, [8, 15], [16, 31]),
  text([15, 32], [31, 48]),
];

describe("toCodePoints", () => {
  it("counts the code points before a UTF-16 offset", () => {
    expect(toCodePoints("🚀 Max", 0)).toBe(0);
    expect(toCodePoints("🚀 Max", 2)).toBe(1); // after the rocket's two UTF-16 units
    expect(toCodePoints("🚀 Max", 3)).toBe(2);
    expect(toCodePoints("👋🏽ab", 5)).toBe(3);
  });
});

describe("toRawRange", () => {
  it("toRawRange maps a selection after a length-changing replacement", () => {
    // the stripped phone `+49 170 1234567` is [17, 32): the raw phone [33, 48)
    expect(toRawRange(BRIEF_MAP, 17, 32)).toEqual({ start: 33, end: 48 });
    // a part of it maps character for character
    expect(toRawRange(BRIEF_MAP, 21, 24)).toEqual({ start: 37, end: 40 });
  });

  it("a boundary inside a replacement snaps to its raw bounds", () => {
    // from inside [EMAIL] to inside the phone: the whole e-mail address and the phone's start
    expect(toRawRange(BRIEF_MAP, 10, 20)).toEqual({ start: 16, end: 36 });
    // from the text before [NAME] to inside it: the whole name
    expect(toRawRange(BRIEF_MAP, 0, 3)).toEqual({ start: 0, end: 14 });
    // a selection that ends at the replacement's last character takes the whole contact
    expect(toRawRange(BRIEF_MAP, 6, 15)).toEqual({ start: 14, end: 31 });
    // one that ends right before it, or starts right after it, does not
    expect(toRawRange(BRIEF_MAP, 6, 8)).toEqual({ start: 14, end: 16 });
    expect(toRawRange(BRIEF_MAP, 15, 17)).toEqual({ start: 31, end: 33 });
  });

  it("an empty selection, one outside the map, or no map maps to nothing", () => {
    expect(toRawRange(BRIEF_MAP, 5, 5)).toBeNull();
    expect(toRawRange(BRIEF_MAP, 30, 40)).toBeNull();
    expect(toRawRange([], 0, 3)).toBeNull();
  });
});

describe("the edit set", () => {
  it("marks and restores a span after an emoji by code points", () => {
    // `🚀 Max Mustermann`: the name starts at UTF-16 offset 3, code point 2
    const before = reviewDraft("emoji", 0);
    const preview = before.stripped_text_preview ?? "";
    const start = toCodePoints(preview, preview.indexOf("Max"));
    const end = toCodePoints(preview, preview.indexOf("Mustermann") + "Mustermann".length);
    expect([start, end]).toEqual([2, 16]);
    const raw = toRawRange(before.segments, start, end);
    expect(raw).toEqual({ start: 2, end: 16 });
    const marked = withRemoval(editSetOf(before.edits), raw ?? { start: 0, end: 0 });
    expect(marked).toEqual(sentEdits("emoji", 1));
    // the server's [REMOVED] in its place: restoring it drops the removal
    const after = reviewDraft("emoji", 1);
    const segment = after.segments.find((s) => s.kind === "removed");
    expect(segment).toBeDefined();
    expect(withRestore(editSetOf(after.edits), segment ?? after.segments[0])).toEqual(sentEdits("emoji", 2));
  });

  it("editSetOf sends the stored set back: restores, then removals, by span", () => {
    expect(editSetOf({ restored: [{ start: 0, end: 14 }], removed: [{ start: 46, end: 59 }, { start: 72, end: 82 }] }))
      .toEqual([
        { op: "restore", start: 0, end: 14 },
        { op: "remove", start: 46, end: 59 },
        { op: "remove", start: 72, end: 82 },
      ]);
    expect(editSetOf({ restored: [], removed: [] })).toEqual([]);
  });

  it("a removal overlapping a stored one is merged", () => {
    const set = editSetOf({ restored: [{ start: 0, end: 14 }], removed: [{ start: 20, end: 30 }, { start: 40, end: 50 }] });
    // overlaps both: one removal over their union, after the restore
    expect(withRemoval(set, { start: 25, end: 45 })).toEqual([
      { op: "restore", start: 0, end: 14 },
      { op: "remove", start: 20, end: 50 },
    ]);
    // inside one: that one
    expect(withRemoval(set, { start: 22, end: 24 })).toEqual([
      { op: "restore", start: 0, end: 14 },
      { op: "remove", start: 40, end: 50 },
      { op: "remove", start: 20, end: 30 },
    ]);
    // touching ones stay apart (the server merges them in the text, and refuses only an overlap)
    expect(withRemoval(set, { start: 30, end: 40 })).toEqual([
      ...set,
      { op: "remove", start: 30, end: 40 },
    ]);
  });

  it("a restore by replacement_index or by raw span", () => {
    const stored = editSetOf({ restored: [], removed: [{ start: 60, end: 70 }] });
    // a replacement of the fresh strip: by its index
    expect(withRestore(stored, replacement("email", 3, [8, 15], [16, 31]))).toEqual([
      ...stored,
      { op: "restore", replacement_index: 3 },
    ]);
    // a contact inside a restored personal span has no index: by its raw span
    expect(withRestore(stored, replacement("phone", null, [20, 27], [40, 55]))).toEqual([
      ...stored,
      { op: "restore", start: 40, end: 55 },
    ]);
    // a [REMOVED]: the stored removals inside its raw span go (it covers what a removal merged with)
    const set = editSetOf({
      restored: [{ start: 0, end: 14 }],
      removed: [{ start: 20, end: 25 }, { start: 24, end: 30 }, { start: 60, end: 70 }],
    });
    expect(withRestore(set, removed([10, 19], [20, 31]))).toEqual([
      { op: "restore", start: 0, end: 14 },
      { op: "remove", start: 60, end: 70 },
    ]);
  });

  it("successive edits map through the refreshed segments", () => {
    // mark the phone with the first answer's map
    const first = reviewDraft("successive", 0);
    const preview = first.stripped_text_preview ?? "";
    const phoneAt = toCodePoints(preview, preview.indexOf("0-170"));
    const phone = toRawRange(first.segments, phoneAt, phoneAt + "0-170-1234567".length);
    expect(withRemoval(editSetOf(first.edits), phone ?? { start: 0, end: 0 })).toEqual(sentEdits("successive", 1));
    // then the date of birth with the second answer's (the [REMOVED] before it is shorter than the phone)
    const second = reviewDraft("successive", 1);
    const next = second.stripped_text_preview ?? "";
    const dobAt = toCodePoints(next, next.indexOf("01.02.1990"));
    const dob = toRawRange(second.segments, dobAt, dobAt + "01.02.1990".length);
    expect(withRemoval(editSetOf(second.edits), dob ?? { start: 0, end: 0 })).toEqual(sentEdits("successive", 2));
  });

  it("holds at most the server's number of edits", () => {
    expect(MAX_SPAN_EDITS).toBe(1000);
  });
});
