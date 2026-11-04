// An ad's description with its evidence quotes marked (spec §15.4; plan M4 Task 24, resolution c, Rulings T6-f
// and B14). The stored description holds one paragraph per line, joined by a single "\n" (`normalize/html.py`),
// so each line is a paragraph; the spans are code-point offsets into the whole text, "\n" included, and a span
// across a line break is marked in each paragraph it touches. The text is only ever text: each paragraph is a
// <p> of text nodes and <mark>s, never HTML. `lang` is the ad's language, or none (the page's applies).
import { Fragment } from "react";
import { mergeSpans, type Piece, type Span, splitBySpans } from "./codepoints";
import styles from "./EvidenceHighlighter.module.css";

export interface EvidenceHighlighterProps {
  text: string;
  lang: "de" | "en" | null;
  spans: readonly Span[];
}

interface Paragraph {
  /** Its first code point's offset in the text (its key). */
  start: number;
  pieces: Piece[];
}

/** The text's non-empty lines, each cut by the parts of `spans` that fall inside it. */
function paragraphs(text: string, spans: readonly Span[]): Paragraph[] {
  const points = Array.from(text);
  const merged = mergeSpans(spans, points.length);
  const found: Paragraph[] = [];
  let start = 0;
  for (let at = 0; at <= points.length; at++) {
    if (at < points.length && points[at] !== "\n") {
      continue;
    }
    if (at > start) {
      const local = merged
        .filter((span) => span.end > start && span.start < at)
        .map((span) => ({ start: Math.max(span.start, start) - start, end: Math.min(span.end, at) - start }));
      found.push({ start, pieces: splitBySpans(points.slice(start, at).join(""), local) });
    }
    start = at + 1;
  }
  return found;
}

export function EvidenceHighlighter({ text, lang, spans }: EvidenceHighlighterProps) {
  return (
    <div className={styles.text} lang={lang ?? undefined}>
      {paragraphs(text, spans).map((paragraph) => (
        <p key={paragraph.start}>
          {paragraph.pieces.map((piece, i) =>
            piece.marked ? <mark key={i} className={styles.mark}>{piece.text}</mark> : <Fragment key={i}>{piece.text}</Fragment>,
          )}
        </p>
      ))}
    </div>
  );
}
