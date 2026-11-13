import { describe, expect, it } from "vitest";
import doc from "../../../docs/PRIVACY.md?raw";
import { type Block, fillOperator, OPERATOR_MARKERS, type OperatorValues, parsePrivacyDoc } from "./privacyDoc";

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

const VALUES: OperatorValues = {
  operator: "Test Operator",
  contact: "operator@example.org",
  controller: "Test Controller",
  vps_provider: "Test VPS Provider",
  s3_provider: "Test S3 Provider",
};

describe("fillOperator", () => {
  it("knows each marker of the statement, in its order, and each of the operator file's keys once", () => {
    expect(doc.match(/\[set at deployment:[^\]\n]*\]/g)).toEqual(Object.keys(OPERATOR_MARKERS));
    expect(Object.values(OPERATOR_MARKERS)).toEqual(["operator", "contact", "controller", "vps_provider", "s3_provider"]);
  });

  it("puts each value in place of its marker, in text only", () => {
    const blocks = parsePrivacyDoc(
      [
        "## Operator",
        "",
        "These values come from the operator file.",
        "",
        "- **Operator:** [set at deployment: operator name]",
        "- **Contact address:** [set at deployment: contact address] (or `[set at deployment: contact address]`)",
        "",
        "| Controller | VPS | S3 |",
        "|---|---|---|",
        "| [set at deployment: controller name] | [set at deployment: VPS provider] | [set at deployment: S3 backup provider] |",
        "",
        "Unknown: [set at deployment: something else]",
      ].join("\n"),
    );
    const filled = fillOperator(blocks, VALUES);
    expect(filled).toEqual([
      blocks[0],
      blocks[1],
      {
        kind: "ul",
        items: [
          [{ kind: "strong", text: "Operator:" }, { kind: "text", text: " Test Operator" }],
          [
            { kind: "strong", text: "Contact address:" },
            { kind: "text", text: " operator@example.org (or " },
            // a code span is the document's own text: never filled
            { kind: "code", text: "[set at deployment: contact address]" },
            { kind: "text", text: ")" },
          ],
        ],
      },
      {
        kind: "table",
        head: [[{ kind: "text", text: "Controller" }], [{ kind: "text", text: "VPS" }], [{ kind: "text", text: "S3" }]],
        rows: [[[{ kind: "text", text: "Test Controller" }], [{ kind: "text", text: "Test VPS Provider" }], [{ kind: "text", text: "Test S3 Provider" }]]],
      },
      // a marker the statement does not have stays as it is
      { kind: "p", inlines: [{ kind: "text", text: "Unknown: [set at deployment: something else]" }] },
    ]);
    // the parsed blocks are left as they were
    expect(JSON.stringify(blocks)).toContain("[set at deployment: operator name]");
  });

  it("keeps the markers without values, and a value's own characters as they are", () => {
    const blocks = parsePrivacyDoc("- **Operator:** [set at deployment: operator name] / [set at deployment: VPS provider]");
    expect(fillOperator(blocks, null)).toEqual(blocks);
    const odd: OperatorValues = { ...VALUES, operator: "$& **not bold** `no code` <b>no tag</b>", vps_provider: null };
    expect(fillOperator(blocks, odd)).toEqual([
      {
        kind: "ul",
        items: [
          [
            { kind: "strong", text: "Operator:" },
            { kind: "text", text: " $& **not bold** `no code` <b>no tag</b> / [set at deployment: VPS provider]" },
          ],
        ],
      },
    ]);
  });
});
