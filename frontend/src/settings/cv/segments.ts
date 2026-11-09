// The text review's offsets (spec §12.3; plan M4 Task 27, resolution f).
//
// The user selects text in the **stripped** text (`stripped_text_preview`), while `PUT …/text` takes the
// complete edit set as spans of the **raw** text, which the client never sees (`api/routes/profile_review.py`).
// The review map (`segments`) partitions both: each run of the stripped text with the raw span it stands for.
// Every offset is a Unicode code point, as Python indexes a `str`, so a selection's UTF-16 offsets are converted
// first (`toCodePoints`, over `Array.from`). A PUT answers only the stripped text, so after each one the draft is
// read again and the next selection maps through its fresh `segments` and `edits`.
import type { components } from "../../api/schema";

export type SegmentOut = components["schemas"]["SegmentOut"];
export type TextEditsOut = components["schemas"]["TextEditsOut"];
export type SpanEdit = components["schemas"]["SpanEditIn"];

/** A half-open span `[start, end)` of the raw text, in code points. */
export interface RawSpan {
  start: number;
  end: number;
}

/** The most edits one set may hold (`MAX_SPAN_EDITS`, `api/routes/profile_review.py`). */
export const MAX_SPAN_EDITS = 1000;

/** The code points of `text` before its UTF-16 offset `utf16Offset`. */
export function toCodePoints(text: string, utf16Offset: number): number {
  return Array.from(text.slice(0, utf16Offset)).length;
}

/**
 * The raw span behind the stripped code points `[start, end)`, or `null` for an empty selection or one the map
 * does not cover (no map at all included). A boundary inside a `text` run maps by its offset (the same
 * characters); one that takes in any character of a `replacement` or a `removed` run snaps outward to that run's
 * raw bounds (a start to its `raw_start`, an end to its `raw_end`), so a selection never splits a token and
 * covers the whole raw contact behind it.
 */
export function toRawRange(segments: readonly SegmentOut[], start: number, end: number): RawSpan | null {
  if (start >= end) {
    return null;
  }
  const first = segments.find((s) => s.stripped_start <= start && start < s.stripped_end);
  const last = segments.find((s) => s.stripped_start < end && end <= s.stripped_end);
  if (first === undefined || last === undefined) {
    return null;
  }
  return {
    start: first.kind === "text" ? first.raw_start + (start - first.stripped_start) : first.raw_start,
    end: last.kind === "text" ? last.raw_start + (end - last.stripped_start) : last.raw_end,
  };
}

/** The draft's stored set as an edit set to send back (a PUT replaces the whole set): each restored replacement
 * as a `restore` by its span, then each removed span as a `remove`. */
export function editSetOf(edits: TextEditsOut): SpanEdit[] {
  return [
    ...edits.restored.map(({ start, end }): SpanEdit => ({ op: "restore", start, end })),
    ...edits.removed.map(({ start, end }): SpanEdit => ({ op: "remove", start, end })),
  ];
}

function isRemoval(edit: SpanEdit): edit is SpanEdit & { start: number; end: number } {
  return edit.op === "remove" && typeof edit.start === "number" && typeof edit.end === "number";
}

/** `set` with the removal of `raw`, unioned with every removal of the set it overlaps (the server refuses
 * overlapping removals; touching ones stay apart), which leave the set; the removal goes last. */
export function withRemoval(set: readonly SpanEdit[], raw: RawSpan): SpanEdit[] {
  const merged = { ...raw };
  const taken = new Set<SpanEdit>();
  let grown = true;
  while (grown) {
    grown = false;
    for (const edit of set) {
      if (!taken.has(edit) && isRemoval(edit) && edit.start < merged.end && merged.start < edit.end) {
        merged.start = Math.min(merged.start, edit.start);
        merged.end = Math.max(merged.end, edit.end);
        taken.add(edit);
        grown = true;
      }
    }
  }
  return [...set.filter((edit) => !taken.has(edit)), { op: "remove", start: merged.start, end: merged.end }];
}

/** `set` with `segment` put back: a restorable `replacement` as a `restore` by its `replacement_index`, or by
 * its raw span when it has none (a contact inside a restored personal span); a `removed` run by dropping the
 * removals inside its raw span (it covers the union of a removal and what it merged with). Any other segment
 * leaves the set as it is. */
export function withRestore(set: readonly SpanEdit[], segment: SegmentOut): SpanEdit[] {
  if (segment.kind === "removed") {
    return set.filter((edit) => !(isRemoval(edit) && edit.start >= segment.raw_start && edit.end <= segment.raw_end));
  }
  if (segment.kind !== "replacement" || !segment.restorable) {
    return [...set];
  }
  const restore: SpanEdit =
    segment.replacement_index === null
      ? { op: "restore", start: segment.raw_start, end: segment.raw_end }
      : { op: "restore", replacement_index: segment.replacement_index };
  return [...set, restore];
}
