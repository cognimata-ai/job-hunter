// The criteria of a score (spec §15.4; plan M4 Task 24, resolution d): a table of the six LLM criteria, then
// the two deterministic ones, in the served order, each with its weight, level, basis and evidence.
//
// - A weight or a level the result lacks reads "—".
// - Evidence quotes are in the UI language's quotation marks; a profile row ends "⇄ <its section>", a
//   structured-field row "⇄ <its field>" (the field's name is the server's, verbatim in a <code>).
// - A deterministic criterion shows its inputs as "key: value" (the keys are the server's, verbatim): a scalar as
//   its text, an array joined, an object as "key value" pairs, a null as "—". `compat`'s keys are work modes,
//   named by the card's work-mode texts (a mode outside them stays verbatim in a <code>).
// - A criterion whose fields were corrected (`corrected_fields`) or shortened (`shortened_fields`, B22) is
//   marked so; a corrected or shortened path that names no criterion and no summary is listed under the table.
// - In a narrow drawer each row is restacked (name; weight, level and basis with their headers; evidence) by a
//   container query. The explicit table roles keep the table's semantics, which a browser may drop from an
//   element whose `display` changes.
import { Fragment } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { useServerText } from "../i18n/index";
import styles from "./CriteriaTable.module.css";
import type { DetailScoreOut, EvidenceOut } from "./detailApi";
import { workModeLabel } from "./format";

const SUMMARY_PATHS: ReadonlySet<string> = new Set(["summary_en", "summary_de"]);
/** `location_work_mode`'s compatibility by work mode (`scoring/det_criteria.py`). */
const COMPAT = "compat";

/** Whether a corrected or shortened `path` lies under criterion `key` (`criteria.<key>`, `criteria.<key>.…`). */
function under(path: string, key: string): boolean {
  const prefix = `criteria.${key}`;
  return path === prefix || path.startsWith(`${prefix}.`) || path.startsWith(`${prefix}[`);
}

/** The paths that name neither a criterion nor a summary (the summary is marked beside itself). */
function otherPaths(paths: readonly string[]): string[] {
  return paths.filter((path) => !path.startsWith("criteria.") && !SUMMARY_PATHS.has(path));
}

/** A deterministic input's value (any JSON value, null included) as text. */
function valueText(value: unknown, none: string): string {
  if (value === null || value === undefined) {
    return none;
  }
  if (Array.isArray(value)) {
    return value.map((item: unknown) => valueText(item, none)).join(", ");
  }
  if (typeof value === "object") {
    return Object.entries(value).map(([key, item]) => `${key} ${valueText(item, none)}`).join(", ");
  }
  return typeof value === "string" ? value : JSON.stringify(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `compat`'s levels by work mode: "on-site 2, remote —". */
function Compat({ levels, none }: { levels: Record<string, unknown>; none: string }) {
  const { t } = useTranslation();
  return (
    <>
      {Object.entries(levels).map(([mode, level], i) => (
        <Fragment key={mode}>
          {i > 0 && ", "}
          {workModeLabel(mode, t) ?? <code className={styles.code}>{mode}</code>} {valueText(level, none)}
        </Fragment>
      ))}
    </>
  );
}

function Quote({ evidence }: { evidence: EvidenceOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const quote = t("drawer.quote", { quote: evidence.quote });
  const field = evidence.field_name;
  if (evidence.source_kind === "description" || field === null) {
    return <>{quote}</>;
  }
  const profile = evidence.source_kind === "profile";
  return (
    <>
      {quote} <span aria-hidden="true">⇄</span>{" "}
      <span className="visually-hidden">{t(profile ? "drawer.side.profile" : "drawer.side.structured")} </span>
      {profile ? serverText("profile_section", field) : <code className={styles.code}>{field}</code>}
    </>
  );
}

function Markers({ criterion, score }: { criterion: string; score: DetailScoreOut }) {
  const { t } = useTranslation();
  return (
    <>
      {score.corrected_fields.some((path) => under(path, criterion)) && (
        <>
          {" "}
          <span className={styles.marker}>{t("drawer.corrected")}</span>
        </>
      )}
      {score.shortened_fields.some((path) => under(path, criterion)) && (
        <>
          {" "}
          <span className={styles.marker}>{t("badge.shortened")}</span>
        </>
      )}
    </>
  );
}

function PathList({ label, paths }: { label: string; paths: readonly string[] }) {
  if (paths.length === 0) {
    return null;
  }
  return (
    <p className={styles.paths}>
      {label}:{" "}
      {paths.map((path, i) => (
        <span key={path}>
          {i > 0 && ", "}
          <code className={styles.code}>{path}</code>
        </span>
      ))}
    </p>
  );
}

export function CriteriaTable({ score }: { score: DetailScoreOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const none = t("drawer.no_value");
  const number = (n: number | null) => (n === null ? none : String(n));
  const weight = t("drawer.col.weight");
  const level = t("drawer.col.level");
  const basis = t("drawer.col.basis");
  return (
    <div className={styles.wrap}>
      <table role="table" className={styles.table}>
        <caption className={styles.caption}>{t("drawer.criteria")}</caption>
        <thead role="rowgroup" className={styles.head}>
          <tr role="row">
            <th role="columnheader" scope="col">{t("drawer.col.criterion")}</th>
            <th role="columnheader" scope="col" className={styles.num}>{weight}</th>
            <th role="columnheader" scope="col" className={styles.num}>{level}</th>
            <th role="columnheader" scope="col">{basis}</th>
            <th role="columnheader" scope="col">{t("drawer.col.evidence")}</th>
          </tr>
        </thead>
        <tbody role="rowgroup">
          {score.criteria.map((criterion) => (
            <tr key={criterion.criterion} role="row" className={styles.row}>
              <th role="rowheader" scope="row" className={styles.name}>
                {serverText("criterion", criterion.criterion)}
                <Markers criterion={criterion.criterion} score={score} />
              </th>
              <td role="cell" className={cx(styles.num, styles.weight)} data-label={weight}>{number(criterion.weight)}</td>
              <td role="cell" className={cx(styles.num, styles.level)} data-label={level}>{number(criterion.level)}</td>
              <td role="cell" className={styles.basis} data-label={basis}>{serverText("basis", criterion.basis)}</td>
              <td role="cell" className={styles.evidenceCell}>
                {criterion.evidence.length === 0 ? (
                  none
                ) : (
                  <ul role="list" className={styles.evidence}>
                    {criterion.evidence.map((evidence, i) => (
                      <li key={i}>
                        <Quote evidence={evidence} />
                      </li>
                    ))}
                  </ul>
                )}
              </td>
            </tr>
          ))}
          {score.det_criteria.map((criterion) => {
            const inputs: [string, unknown][] = Object.entries(criterion.inputs);
            return (
              <tr key={criterion.criterion} role="row" className={styles.row}>
                <th role="rowheader" scope="row" className={styles.name}>
                  {serverText("criterion", criterion.criterion)}
                  <Markers criterion={criterion.criterion} score={score} />
                </th>
                <td role="cell" className={cx(styles.num, styles.weight)} data-label={weight}>{number(criterion.weight)}</td>
                <td role="cell" className={cx(styles.num, styles.level)} data-label={level}>{number(criterion.level)}</td>
                <td role="cell" className={styles.basis} data-label={basis}>{serverText("basis", criterion.basis)}</td>
                <td role="cell" className={styles.evidenceCell}>
                  {inputs.length === 0 ? (
                    none
                  ) : (
                    <ul role="list" className={styles.evidence}>
                      {inputs.map(([key, value]) => (
                        <li key={key}>
                          <code className={styles.code}>{key}</code>:{" "}
                          {key === COMPAT && isRecord(value) ? <Compat levels={value} none={none} /> : valueText(value, none)}
                        </li>
                      ))}
                    </ul>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <PathList label={t("drawer.other_corrected")} paths={otherPaths(score.corrected_fields)} />
      <PathList label={t("drawer.other_shortened")} paths={otherPaths(score.shortened_fields)} />
    </div>
  );
}
