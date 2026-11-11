import { describe, expect, it } from "vitest";
import { type Block, parsePrivacyDoc } from "./privacyDoc";

describe("parsePrivacyDoc", () => {
  it("parses headings, paragraphs, lists and tables", () => {
    const md = [
      "An opening paragraph with **bold** text",
      "that goes on with `code`.",
      "",
      "## 1. Purpose and controller",
      "",
      "- **Purpose.** You run Job Hunter.",
      "- A plain item with `a | b` in code",
      "",
      "### Details",
      "",
      "| Third party | What it receives |",
      "|---|---|",
      "| Anthropic (Claude API) | The CV text **and the user's review** |",
      "| ntfy | Alert texts with `a|b` |",
      "",
      "**When a deletion is done.**",
      "",
      "## 1. Purpose and controller",
    ].join("\n");
    const expected: Block[] = [
      {
        kind: "p",
        inlines: [
          { kind: "text", text: "An opening paragraph with " },
          { kind: "strong", text: "bold" },
          { kind: "text", text: " text that goes on with " },
          { kind: "code", text: "code" },
          { kind: "text", text: "." },
        ],
      },
      { kind: "h2", text: "1. Purpose and controller", id: "1-purpose-and-controller" },
      {
        kind: "ul",
        items: [
          [{ kind: "strong", text: "Purpose." }, { kind: "text", text: " You run Job Hunter." }],
          [{ kind: "text", text: "A plain item with " }, { kind: "code", text: "a | b" }, { kind: "text", text: " in code" }],
        ],
      },
      { kind: "h3", text: "Details", id: "details" },
      {
        kind: "table",
        head: [[{ kind: "text", text: "Third party" }], [{ kind: "text", text: "What it receives" }]],
        rows: [
          [
            [{ kind: "text", text: "Anthropic (Claude API)" }],
            [{ kind: "text", text: "The CV text " }, { kind: "strong", text: "and the user's review" }],
          ],
          // a pipe inside a code span is no cell border
          [[{ kind: "text", text: "ntfy" }], [{ kind: "text", text: "Alert texts with " }, { kind: "code", text: "a|b" }]],
        ],
      },
      { kind: "p", inlines: [{ kind: "strong", text: "When a deletion is done." }] },
      // a repeated heading gets an id of its own
      { kind: "h2", text: "1. Purpose and controller", id: "1-purpose-and-controller-2" },
    ];
    expect(parsePrivacyDoc(md)).toEqual(expected);
    // Windows line ends and a trailing newline change nothing
    expect(parsePrivacyDoc(`${md.replace(/\n/g, "\r\n")}\r\n`)).toEqual(expected);
  });

  it("renders unknown syntax as text", () => {
    const lines = [
      "# A level-1 heading",
      "#### A level-4 heading",
      "> a block quote",
      "1. an ordered item",
      "* a star bullet",
      "<script>alert(1)</script>",
      '<img src="x" onerror="alert(1)">',
      "[a link](https://example.com) and &amp; an entity",
      "| a table row | without its rule |",
      "an unclosed **bold and an unclosed `tick",
      "an empty ****",
    ];
    const blocks = parsePrivacyDoc(lines.join("\n\n"));
    // each line its own paragraph of its text, character for character: no markup is read but `**` and `` ` ``
    expect(blocks).toEqual(lines.map((line) => ({ kind: "p", inlines: [{ kind: "text", text: line }] })));
  });
});
