// System → Score cycles (spec §7.6, §14.2 L5036, §15.7; plan M4 Task 31, resolution e): the score cycles, newest
// first, keyset-paged with Load more (`GET /api/score-cycles`): each its trigger, its status (with its reason
// code), when it started, how long it took, the targets it admitted (over its lanes) and what it spent.
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useServerText } from "../i18n/index";
import tables from "../settings/tables.module.css";
import styles from "./CyclesList.module.css";
import { useFormat } from "./format";
import { LoadMore, useElapsed } from "./RunsList";
import parts from "./SystemSection.module.css";
import { type ScoreCycleOut, useCycles } from "./systemApi";

function admitted(cycle: ScoreCycleOut): number {
  return Object.values(cycle.admitted).reduce((sum, n) => sum + n, 0);
}

function CycleRow({ cycle }: { cycle: ScoreCycleOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const elapsed = useElapsed();
  return (
    <tr>
      <th scope="row" className={cx(tables.nowrap, styles.id)}>{t("runs.id", { id: String(cycle.id) })}</th>
      <td data-label={t("cycles.col_trigger")}>{serverText("cycle_trigger", cycle.trigger)}</td>
      <td data-label={t("cycles.col_status")}>
        {serverText("cycle_status", cycle.status)}
        {cycle.status_reason !== null && <code className={styles.reason}>{cycle.status_reason}</code>}
      </td>
      <td data-label={t("cycles.col_started")} className={tables.nowrap}>{format.dateTimeMinutes(cycle.started_at)}</td>
      <td data-label={t("cycles.col_duration")} className={tables.nowrap}>
        {elapsed(cycle.started_at, cycle.finished_at, serverText("cycle_status", "running"))}
      </td>
      <td data-label={t("cycles.col_admitted")} className={tables.number}>{format.number(admitted(cycle))}</td>
      <td data-label={t("cycles.col_spent")} className={tables.number}>{format.usd(cycle.spent_usd)}</td>
    </tr>
  );
}

export function CyclesList() {
  const { t } = useTranslation();
  const cycles = useCycles();
  let body;
  if (cycles.items === undefined) {
    body = cycles.error === null ? <Skeleton variant="line" count={3} /> : <ProblemBox error={cycles.error} onRetry={cycles.retry} />;
  } else if (cycles.items.length === 0) {
    body = <p className={parts.empty}>{t("cycles.empty")}</p>;
  } else {
    body = (
      <>
        <div className={tables.scroll}>
          <table className={tables.table} aria-labelledby="cycles-heading">
            <thead>
              <tr>
                <th scope="col">{t("cycles.col_cycle")}</th>
                <th scope="col">{t("cycles.col_trigger")}</th>
                <th scope="col">{t("cycles.col_status")}</th>
                <th scope="col">{t("cycles.col_started")}</th>
                <th scope="col">{t("cycles.col_duration")}</th>
                <th scope="col" className={tables.number}>{t("cycles.col_admitted")}</th>
                <th scope="col" className={tables.number}>{t("cycles.col_spent")}</th>
              </tr>
            </thead>
            <tbody>
              {cycles.items.map((cycle) => <CycleRow key={cycle.id} cycle={cycle} />)}
            </tbody>
          </table>
        </div>
        <LoadMore list={cycles} />
      </>
    );
  }
  return (
    <section id="score-cycles" aria-labelledby="cycles-heading" className={parts.part}>
      <h3 id="cycles-heading" className={parts.title}>{t("settings_system.cycles")}</h3>
      {body}
    </section>
  );
}
