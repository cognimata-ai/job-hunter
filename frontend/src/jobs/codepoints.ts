// Text by code points (plan M4 Task 24, resolution c): the evidence spans of a description are offsets in code
// points (Python's `str` indexing, `jobs/detail.py`), while a JavaScript string indexes UTF-16 units, so an
// emoji before a quote would shift `String.prototype.slice` by one. Everything here works on `Array.from(text)`,
// one element per code point.

/** A half-open span `[start, end)` of code points. */
export interface Span {
  start: number;
  end: number;
}

/** A run of text, marked when a span covers it. */
export interface Piece {
  text: string;
  marked: boolean;
}

/** The code points `[start, end)` of `text`. */
export function sliceByCodePoints(text: string, start: number, end: number): string {
  return Array.from(text).slice(start, end).join("");
}

/** The spans that lie within `[0, length]` and are not empty, sorted, with overlapping and touching spans merged
 * into one; a span that reaches outside the text is dropped, not clipped. */
export function mergeSpans(spans: readonly Span[], length: number): Span[] {
  const valid = spans
    .filter(({ start, end }) => Number.isInteger(start) && Number.isInteger(end) && start >= 0 && end <= length && start < end)
    .sort((a, b) => a.start - b.start || a.end - b.end);
  const merged: Span[] = [];
  for (const span of valid) {
    const last = merged.at(-1);
    if (last !== undefined && span.start <= last.end) {
      last.end = Math.max(last.end, span.end);
    } else {
      merged.push({ ...span });
    }
  }
  return merged;
}

/** `text` cut into its pieces outside and inside `spans` (code-point offsets, `mergeSpans`'s rules), in order;
 * no empty piece. */
export function splitBySpans(text: string, spans: readonly Span[]): Piece[] {
  const points = Array.from(text);
  const pieces: Piece[] = [];
  const push = (start: number, end: number, marked: boolean) => {
    if (start < end) {
      pieces.push({ text: points.slice(start, end).join(""), marked });
    }
  };
  let at = 0;
  for (const { start, end } of mergeSpans(spans, points.length)) {
    push(at, start, false);
    push(start, end, true);
    at = end;
  }
  push(at, points.length, false);
  return pieces;
}
