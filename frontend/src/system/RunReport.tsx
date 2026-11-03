// A run and its report (spec §17.3; plan M4 Task 21, Ruling g), for the header's drawer and Task 31's run
// history. It fetches nothing: the caller reads the run (`useRun`) and passes it in.
//
// The report's five sections in §17.3's order, each as nested definition lists: the headings are translated,
// every inner key is shown verbatim (ids, source keys and counter names), and every value is text (no markup
// from a value is ever rendered). A top-level key outside the five is not shown.
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { useServerText } from "../i18n/index";
import type { JsonValue, PartialCause, RunOut } from "./api";
import { type Format, useFormat } from "./format";
import styles from "./RunReport.module.css";

const SECTIONS = ["sources", "identity", "revisions", "quota", "execution"] as const;

export interface RunReportProps {
  run: RunOut;
  /** The stored report; `null` while none is stored (the run has not ended). */
  report: Record<string, JsonValue> | null;
  partialCauses: PartialCause[];
}

type T = ReturnType<typeof useTranslation>["t"];

function isRecord(value: unknown): value is Record<string, JsonValue> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function scalar(value: JsonValue, t: T, fmt: Format): string {
  if (value === null || value === undefined) {
    return t("value.none");
  }
  if (typeof value === "number") {
    return fmt.number(value);
  }
  if (typeof value === "boolean") {
    return value ? t("value.yes") : t("value.no");
  }
  return typeof value === "string" ? value : JSON.stringify(value);
}

function Value({ value }: { value: JsonValue }) {
  const { t } = useTranslation();
  const fmt = useFormat();
  if (isRecord(value)) {
    const entries = Object.entries(value);
    return entries.length === 0 ? <>{t("value.none")}</> : <Entries entries={entries} />;
  }
  if (Array.isArray(value)) {
    const items = value as JsonValue[];
    if (items.length === 0) {
      return <>{t("value.none")}</>;
    }
    if (items.every((item) => !isRecord(item) && !Array.isArray(item))) {
      return <>{items.map((item) => scalar(item, t, fmt)).join(", ")}</>;
    }
    return (
      <ol className={styles.items}>
        {items.map((item, i) => <li key={i}><Value value={item} /></li>)}
      </ol>
    );
  }
  return <>{scalar(value, t, fmt)}</>;
}

/** Whether `value` is shown as a nested list of its own (a non-empty object, or a list of them). */
function nests(value: JsonValue): boolean {
  if (isRecord(value)) {
    return Object.keys(value).length > 0;
  }
  return Array.isArray(value) && (value as JsonValue[]).some((item) => isRecord(item) || Array.isArray(item));
}

function Entries({ entries }: { entries: [string, JsonValue][] }) {
  // a group of plain counts reads as one wrapping row ("closed 0  opened 0  abandoned 0")
  const flat = entries.every(([, value]) => !nests(value));
  return (
    <dl className={cx(styles.entries, flat && styles.flat)}>
      {entries.map(([key, value]) => (
        <div key={key} className={nests(value) ? styles.nested : styles.entry}>
          <dt className={styles.key}>{key}</dt>
          <dd className={styles.value}><Value value={value} /></dd>
        </div>
      ))}
    </dl>
  );
}

function Section({ name, value }: { name: (typeof SECTIONS)[number]; value: JsonValue }) {
  const { t } = useTranslation();
  const id = useId();
  return (
    <section aria-labelledby={id} className={styles.section}>
      <h3 id={id} className={styles.sectionTitle}>{t(`run_report.section.${name}`)}</h3>
      {isRecord(value) ? <Value value={value} /> : <p className={styles.value}><Value value={value} /></p>}
    </section>
  );
}

export function RunReport({ run, report, partialCauses }: RunReportProps) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const fmt = useFormat();
  const time = (instant: string | null) => (instant === null ? t("value.none") : fmt.dateTime(instant));
  return (
    <div className={styles.report}>
      <dl className={styles.summary}>
        <div className={styles.entry}>
          <dt className={styles.term}>{t("run_report.status")}</dt>
          <dd>{serverText("run_status", run.status)}</dd>
        </div>
        {run.status_reason !== null && (
          <div className={styles.entry}>
            <dt className={styles.term}>{t("run_report.reason")}</dt>
            <dd><code className={styles.code}>{run.status_reason}</code></dd>
          </div>
        )}
        <div className={styles.entry}>
          <dt className={styles.term}>{t("run_report.queued_at")}</dt>
          <dd>{time(run.queued_at)}</dd>
        </div>
        <div className={styles.entry}>
          <dt className={styles.term}>{t("run_report.started_at")}</dt>
          <dd>{time(run.started_at)}</dd>
        </div>
        <div className={styles.entry}>
          <dt className={styles.term}>{t("run_report.finished_at")}</dt>
          <dd>{time(run.finished_at)}</dd>
        </div>
        <div className={styles.entry}>
          <dt className={styles.term}>{t("run_report.partial_causes")}</dt>
          <dd>
            {partialCauses.length === 0 ? t("value.none") : (
              <ul className={styles.causes}>
                {partialCauses.map((cause) => <li key={cause}>{serverText("partial", cause)}</li>)}
              </ul>
            )}
          </dd>
        </div>
      </dl>
      {report === null ? (
        <p className={styles.pending}>{t("run_report.no_report")}</p>
      ) : (
        SECTIONS.filter((name) => name in report).map((name) => (
          <Section key={name} name={name} value={report[name] ?? null} />
        ))
      )}
    </div>
  );
}
