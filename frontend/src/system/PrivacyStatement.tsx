// The privacy statement (spec §20.6; plan M5 Task 20, Deviation 27 / D20; M4 Deviation 35 ends): `docs/PRIVACY.md`,
// bundled at build time (`?raw`), parsed into blocks (`privacyDoc`) and rendered as React elements: no HTML is
// ever built from its text, so it needs no Markdown library and the CSP holds (no inline script or style). It is
// the last part of Profile & Settings, at `/settings#privacy-statement` (no route of its own), under the settings
// sections, where Privacy & data links to it: inside that section's frame its Save would follow the whole
// statement. Its `##` sections are headings a level below its own, each anchored (`privacy-statement-<id>`), with
// a list of contents linking to them. The document is English: in another UI language a note says so, and its
// text is marked `lang="en"`. Its Operator section's markers show the deployment's values once they are read
// (`useOperator`, M5 follow-ups Task F1), as text; until then, and on a server without an operator file, the
// markers themselves.
import { Link } from "@tanstack/react-router";
import { Fragment, useMemo } from "react";
import { useTranslation } from "react-i18next";
import source from "../../../docs/PRIVACY.md?raw";
import { useOperator } from "./privacyApi";
import { type Block, fillOperator, type Inline, parsePrivacyDoc } from "./privacyDoc";
import styles from "./PrivacyStatement.module.css";

export const STATEMENT_ID = "privacy-statement";

const BLOCKS = parsePrivacyDoc(source);
type Heading = Extract<Block, { id: string }>;
/** The document's `##` sections, for its contents. */
const SECTIONS = BLOCKS.filter((block): block is Heading => block.kind === "h2");

function anchorOf(id: string): string {
  return `${STATEMENT_ID}-${id}`;
}

function Inlines({ inlines }: { inlines: Inline[] }) {
  return inlines.map((inline, i) => {
    switch (inline.kind) {
      case "strong":
        return <strong key={i}>{inline.text}</strong>;
      case "code":
        return <code key={i} className={styles.code}>{inline.text}</code>;
      default:
        return <Fragment key={i}>{inline.text}</Fragment>;
    }
  });
}

function BlockView({ block }: { block: Block }) {
  switch (block.kind) {
    case "h2":
      return <h3 id={anchorOf(block.id)} className={styles.h2}>{block.text}</h3>;
    case "h3":
      return <h4 id={anchorOf(block.id)} className={styles.h3}>{block.text}</h4>;
    case "p":
      return <p className={styles.p}><Inlines inlines={block.inlines} /></p>;
    case "ul":
      return (
        <ul className={styles.ul}>
          {block.items.map((item, i) => <li key={i}><Inlines inlines={item} /></li>)}
        </ul>
      );
    case "table":
      return (
        <div className={styles.scroll}>
          <table className={styles.table}>
            <thead>
              <tr>{block.head.map((cell, i) => <th key={i} scope="col"><Inlines inlines={cell} /></th>)}</tr>
            </thead>
            <tbody>
              {block.rows.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, i) => (
                    <td key={i} data-label={block.head[i]?.map((inline) => inline.text).join("")}><Inlines inlines={cell} /></td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
  }
}

/** Blocks of the statement as elements; text is only ever text. */
export function StatementBlocks({ blocks }: { blocks: Block[] }) {
  return blocks.map((block, i) => <BlockView key={i} block={block} />);
}

export function PrivacyStatement() {
  const { t, i18n } = useTranslation();
  const operator = useOperator();
  const blocks = useMemo(() => fillOperator(BLOCKS, operator.data ?? null), [operator.data]);
  return (
    <section id={STATEMENT_ID} aria-labelledby={`${STATEMENT_ID}-heading`} className={styles.statement}>
      <h2 id={`${STATEMENT_ID}-heading`} className={styles.title}>{t("privacy_statement.title")}</h2>
      {i18n.language !== "en" && <p className={styles.note}>{t("privacy_statement.english_only")}</p>}
      <nav aria-label={t("privacy_statement.contents")} className={styles.contents}>
        <ol lang="en" className={styles.contentsList}>
          {SECTIONS.map((block) => (
            <li key={block.id}>
              <Link to="/settings" hash={anchorOf(block.id)}>{block.text}</Link>
            </li>
          ))}
        </ol>
      </nav>
      <div lang="en" className={styles.doc}>
        <StatementBlocks blocks={blocks} />
      </div>
    </section>
  );
}
