// A query's planned cells (spec §5.5.2, §15.7; plan M4 Task 29, resolution b): the disclosure of a row of the
// queries table, from the one read of `GET /api/queries/stats` the section shares, filtered by the query. Each
// cell is an item of a list (no nested table, so it fits at 390 px): its source, its place (the scope location
// whose key it is, else the key; `remote-de` as remote), its coverage, its yield, its zero-yield and incomplete
// runs, its open gaps, whether it is too broad, and for a JSearch cell the refresh cadence ("—" for 0).
import type { UseQueryResult } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import type { ProblemError } from "../api/client";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { intlLocale, useServerText } from "../i18n/index";
import { type Format, useFormat } from "../system/format";
import type { CellStatsOut } from "./queriesApi";
import styles from "./QueryStats.module.css";
import tables from "./tables.module.css";
import { useSettingsForm } from "./useSettingsForm";

/** The location key of remote work within Germany (`sources.base.REMOTE_DE`). */
export const REMOTE_KEY = "remote-de";

/** The location key of a scope label, as `run_settings.slug` makes it for the common labels (case folded, the
 * accents and every run of other characters dropped); a label it misses shows its key. */
function slugOf(label: string): string {
  return label
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replaceAll("ß", "ss")
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/^-+|-+$/g, "");
}

/** A number with at most two decimals. */
export function decimals(locale: string): (value: number) => string {
  const format = new Intl.NumberFormat(locale, { maximumFractionDigits: 2 });
  return (value) => format.format(value);
}

export interface QueryStatsProps {
  queryId: number;
  stats: UseQueryResult<CellStatsOut[], ProblemError>;
}

function Figure({ term, value }: { term: string; value: string }) {
  return (
    <div className={styles.figure}>
      <dt>{term}</dt>
      <dd>{value}</dd>
    </div>
  );
}

/** A cadence in days: "—" when there is none or it is 0 (Carry T13), else "n days". */
export function cadenceText(days: number | null, t: TFunction, number: (value: number) => string): string {
  return days === null || days === 0 ? t("value.none") : t("settings_queries.days", { count: days, value: number(days) });
}

function Cell({ cell, place, format, number }: { cell: CellStatsOut; place: string; format: Format; number: (value: number) => string }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  return (
    <li className={styles.cell}>
      <div className={styles.head}>
        <span className={styles.source}>{serverText("source", cell.source_id)}</span>
        <span className={styles.place}>{place}</span>
        {cell.too_broad && <span className={tables.warn}>{t("settings_queries.too_broad")}</span>}
      </div>
      <dl className={styles.figures}>
        <Figure term={t("settings_queries.cell.last_covered")}
          value={cell.last_covered_at === null ? t("settings_queries.never") : format.dayMonthTime(cell.last_covered_at)} />
        <Figure term={t("settings_queries.cell.last_success")}
          value={cell.last_success_at === null ? t("settings_queries.never") : format.dayMonthTime(cell.last_success_at)} />
        <Figure term={t("settings_queries.cell.yield")} value={number(cell.yield_ema)} />
        <Figure term={t("settings_queries.cell.zero_yield")} value={format.number(cell.zero_yield_streak)} />
        <Figure term={t("settings_queries.cell.incomplete")} value={format.number(cell.consecutive_incomplete)} />
        <Figure term={t("settings_queries.cell.open_gaps")} value={format.number(cell.open_gaps)} />
        {cell.cadence_days !== null && (
          <Figure term={t("settings_queries.cell.cadence")} value={cadenceText(cell.cadence_days, t, number)} />
        )}
      </dl>
    </li>
  );
}

export function QueryStats({ queryId, stats }: QueryStatsProps) {
  const { t, i18n } = useTranslation();
  const format = useFormat();
  const form = useSettingsForm();
  if (stats.isPending) {
    return <Skeleton variant="line" count={2} />;
  }
  if (stats.isError) {
    return <ProblemBox error={stats.error} onRetry={() => { void stats.refetch(); }} />;
  }
  const cells = stats.data.filter((cell) => cell.query_id === queryId);
  if (cells.length === 0) {
    return <p className={styles.none}>{t("settings_queries.cell.none")}</p>;
  }
  const labels = new Map(form.base.payload.scope.locations.map((location) => [slugOf(location.label), location.label]));
  const placeOf = (key: string) => (key === REMOTE_KEY ? t("settings_queries.cell.remote") : (labels.get(key) ?? key));
  const number = decimals(intlLocale(i18n.language));
  return (
    <ul role="list" className={styles.cells}>
      {cells.map((cell) => (
        <Cell key={cell.cell_id} cell={cell} place={placeOf(cell.location_key)} format={format} number={number} />
      ))}
    </ul>
  );
}
