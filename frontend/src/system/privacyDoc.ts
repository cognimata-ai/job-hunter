// The privacy statement's Markdown (`docs/PRIVACY.md`, plan M5 Task 19, Deviation 27 / D20) as blocks the SPA
// renders as React elements (`PrivacyStatement`): never HTML, so no Markdown library and no HTML injection.
//
// The subset is Task 19's (`tests/unit/test_privacy_md.py` holds the document to it): `##` and `###` headings,
// paragraphs (a run of lines, joined by a space), `- ` bullet items (an indented line continues the item),
// pipe tables (a head row, a rule row of dashes, then rows; a `|` inside a code span is no cell border),
// `**bold**` and `` `code` ``. Anything else is text: a line of any other form (a level-1 or level-4 heading, a
// quote, an ordered item, HTML, a link) is a paragraph of its own characters, and an unclosed `**` or `` ` ``
// is text too. Each heading gets an id from its text (`1. Purpose and controller` → `1-purpose-and-controller`),
// unique within the document.
//
// The Operator section's five markers (`[set at deployment: …]`) stay in the repository; `fillOperator` puts the
// deployment's values in their place (M5 follow-ups Task F1, `GET /api/privacy/operator`), in text spans only,
// after the parse: a value is text, whatever characters it has. While there is no value to show (the read is
// pending or failed), a note of the UI takes each marker's place instead, marked with the UI's language.

/** A span of the document's own text. */
type DocInline = { kind: "text" | "strong" | "code"; text: string };
/** A note of the UI in an operator marker's place (`fillOperator`), in the UI's language (`lang`). */
type NoteInline = { kind: "note"; text: string; lang: string };
export type Inline = DocInline | NoteInline;

export type Block =
  | { kind: "h2" | "h3"; text: string; id: string }
  | { kind: "p"; inlines: Inline[] }
  | { kind: "ul"; items: Inline[][] }
  | { kind: "table"; head: Inline[][]; rows: Inline[][][] };

const HEADING = /^(#{2,3}) (.*)$/;
const BULLET = /^- (.*)$/;
const CONTINUATION = /^\s+(\S.*)$/;
const TABLE_RULE = /^\|(\s*:?-+:?\s*\|)+\s*$/;
const SLUG_GAP = /[^\p{L}\p{N}]+/gu;
/** The start of a line of a form outside the subset (a heading of another level, a quote, an ordered or `*`/`+`
 * item, HTML, a fence, a table row without its rule): a paragraph of its own text. */
const FOREIGN = /^\s*(#|>|\d+[.)](\s|$)|[*+](\s|$)|<|```|~~~|\|)/;

/** `text`'s inline spans: `` `code` `` (its text verbatim), `**bold**` (non-empty), the rest text. */
function parseInlines(text: string): Inline[] {
  const spans: Inline[] = [];
  const push = (kind: DocInline["kind"], part: string) => {
    const last = spans.at(-1);
    if (kind === "text" && last?.kind === "text") {
      last.text += part;
    } else if (part !== "" || kind !== "text") {
      spans.push({ kind, text: part });
    }
  };
  let rest = text;
  while (rest !== "") {
    const tick = rest.indexOf("`");
    const star = rest.indexOf("**");
    const at = [tick, star].filter((i) => i >= 0).reduce((a, b) => Math.min(a, b), rest.length);
    push("text", rest.slice(0, at));
    rest = rest.slice(at);
    if (rest === "") {
      break;
    }
    if (rest.startsWith("`")) {
      const end = rest.indexOf("`", 1);
      if (end > 1) {
        push("code", rest.slice(1, end));
        rest = rest.slice(end + 1);
      } else {
        push("text", "`");
        rest = rest.slice(1);
      }
    } else {
      const end = rest.indexOf("**", 2);
      if (end > 2) {
        push("strong", rest.slice(2, end));
        rest = rest.slice(end + 2);
      } else {
        push("text", "**");
        rest = rest.slice(2);
      }
    }
  }
  return spans;
}

/** A table row's cells: split at each `|` outside a code span, the outer ones dropped, each cell trimmed. */
function cells(line: string): string[] {
  const parts: string[] = [];
  let current = "";
  let inCode = false;
  for (const char of line.trim()) {
    if (char === "`") {
      inCode = !inCode;
    }
    if (char === "|" && !inCode) {
      parts.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  parts.push(current);
  // the text before the first `|` and after the last one
  return parts.slice(1, -1).map((cell) => cell.trim());
}

function isTableRow(line: string): boolean {
  const trimmed = line.trim();
  return trimmed.startsWith("|") && trimmed.endsWith("|") && trimmed.length > 1;
}

function plainText(inlines: Inline[]): string {
  return inlines.map((inline) => inline.text).join("");
}

/** The document's blocks, in order. */
export function parsePrivacyDoc(md: string): Block[] {
  const lines = md.split(/\r?\n/);
  const blocks: Block[] = [];
  const ids = new Map<string, number>();
  const anchor = (text: string): string => {
    const base = text.toLowerCase().replace(SLUG_GAP, "-").replace(/^-+|-+$/g, "") || "section";
    const seen = (ids.get(base) ?? 0) + 1;
    ids.set(base, seen);
    return seen === 1 ? base : `${base}-${String(seen)}`;
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i] ?? "";
    if (line.trim() === "") {
      i++;
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading !== null) {
      const text = plainText(parseInlines(heading[2])).trim();
      blocks.push({ kind: heading[1] === "##" ? "h2" : "h3", text, id: anchor(text) });
      i++;
      continue;
    }
    if (BULLET.test(line)) {
      const items: string[] = [];
      while (i < lines.length) {
        const current = lines[i] ?? "";
        const bullet = BULLET.exec(current);
        const continued = items.length > 0 ? CONTINUATION.exec(current) : null;
        if (bullet !== null) {
          items.push(bullet[1]);
        } else if (continued !== null) {
          items[items.length - 1] += ` ${continued[1]}`;
        } else {
          break;
        }
        i++;
      }
      blocks.push({ kind: "ul", items: items.map(parseInlines) });
      continue;
    }
    if (isTableRow(line) && TABLE_RULE.test((lines[i + 1] ?? "").trim())) {
      const head = cells(line).map(parseInlines);
      const rows: Inline[][][] = [];
      i += 2;
      while (i < lines.length && isTableRow(lines[i] ?? "")) {
        rows.push(cells(lines[i] ?? "").map(parseInlines));
        i++;
      }
      blocks.push({ kind: "table", head, rows });
      continue;
    }
    // a paragraph: a line of a form outside the subset alone; else this line and the plain lines after it, up to
    // a blank line or another block's start
    const text: string[] = [line.trim()];
    i++;
    while (!FOREIGN.test(line) && i < lines.length) {
      const next = lines[i] ?? "";
      if (next.trim() === "" || BULLET.test(next) || FOREIGN.test(next)) {
        break;
      }
      text.push(next.trim());
      i++;
    }
    blocks.push({ kind: "p", inlines: parseInlines(text.join(" ")) });
  }
  return blocks;
}

/** The statement's operator markers, in its order, and the operator file's key whose value takes each one's
 * place (`docs/PRIVACY.md`, under Operator; `tests/unit/test_privacy_md.py` holds the document to them). */
export const OPERATOR_MARKERS = {
  "[set at deployment: operator name]": "operator",
  "[set at deployment: contact address]": "contact",
  "[set at deployment: controller name]": "controller",
  "[set at deployment: VPS provider]": "vps_provider",
  "[set at deployment: S3 backup provider]": "s3_provider",
} as const;

export type OperatorKey = (typeof OPERATOR_MARKERS)[keyof typeof OPERATOR_MARKERS];
/** The operator file's values (`OperatorOut`); `null` for one the server does not have. */
export type OperatorValues = Record<OperatorKey, string | null>;
/** What takes the markers' place: the server's values (a null one keeps its marker, as a server without an
 * operator file answers), or one note of the UI for every marker while there is no answer to show. */
export type OperatorFill = { values: OperatorValues } | { note: string; lang: string };

const MARKER = /\[set at deployment:[^\]\n]*\]/g;
const MARKER_KEYS: ReadonlyMap<string, OperatorKey> = new Map(Object.entries(OPERATOR_MARKERS));

/** `blocks` with each marker in a text span replaced by its value, or by the note; a marker of no key stays, a
 * marker whose value is null stays, and so does a code span (the document's own text). A value is inserted as
 * it is (`replace` reads no pattern in what its function returns); a note becomes a span of its own, which keeps
 * its language. */
export function fillOperator(blocks: Block[], fill: OperatorFill): Block[] {
  const fillText = (text: string): Inline[] => {
    if ("values" in fill) {
      const { values } = fill;
      const filled = text.replace(MARKER, (marker) => {
        const key = MARKER_KEYS.get(marker);
        return (key === undefined ? null : values[key]) ?? marker;
      });
      return [{ kind: "text", text: filled }];
    }
    const parts: Inline[] = [];
    let rest = 0;
    const plain = (end: number) => {
      if (end > rest) {
        parts.push({ kind: "text", text: text.slice(rest, end) });
      }
    };
    for (const match of text.matchAll(MARKER)) {
      if (MARKER_KEYS.has(match[0])) {
        plain(match.index);
        parts.push({ kind: "note", text: fill.note, lang: fill.lang });
        rest = match.index + match[0].length;
      }
    }
    plain(text.length);
    return parts.length > 0 ? parts : [{ kind: "text", text }];
  };
  const fillInlines = (inlines: Inline[]): Inline[] =>
    inlines.flatMap((inline) => (inline.kind === "text" ? fillText(inline.text) : [inline]));
  return blocks.map((block): Block => {
    switch (block.kind) {
      case "p":
        return { kind: "p", inlines: fillInlines(block.inlines) };
      case "ul":
        return { kind: "ul", items: block.items.map(fillInlines) };
      case "table":
        return { kind: "table", head: block.head.map(fillInlines), rows: block.rows.map((row) => row.map(fillInlines)) };
      default:
        return block;
    }
  });
}
