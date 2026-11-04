// System → Golden set (spec §11.13 L4610-4632, §14.2 L4978-4979, §15.7; plan M4 Task 31, resolution e): the
// golden cases by id, keyset-paged with Load more (`GET /api/golden-cases`). Each row shows its title and company
// (the labelled ad's, `title` and `company`, Task 15), its label, its source (a synthetic case of the eval seed
// says so), and its active and canary toggles; each toggle sends `PATCH {active}` or `PATCH {is_canary}` alone
// and is disabled while it runs (a third canary is refused, 422, shown at the row). [Delete] asks in a
// `ConfirmDialog`, then sends `DELETE`. Every write reads the set and the golden runs' gate again.
import { Fragment, useState } from "react";
import { useTranslation } from "react-i18next";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useToast } from "../components/Toaster";
import { useServerText } from "../i18n/index";
import tables from "../settings/tables.module.css";
import styles from "./GoldenSet.module.css";
import { LoadMore } from "./lists";
import { type GoldenCaseOut, useDeleteGoldenCase, useGoldenCases, useSetGoldenFlags } from "./systemApi";
import parts from "./SystemSection.module.css";

const COLUMNS = 7;

function CaseRow({ golden }: { golden: GoldenCaseOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const toast = useToast();
  const flags = useSetGoldenFlags();
  const remove = useDeleteGoldenCase();
  const [asking, setAsking] = useState(false);
  const busy = flags.isPending || remove.isPending;
  const error = flags.error ?? remove.error;
  return (
    <Fragment>
      <tr>
        <td data-label={t("golden.col_title")} className={tables.text}>{golden.title}</td>
        <td data-label={t("golden.col_company")}>{golden.company}</td>
        <td data-label={t("golden.col_label")}>{serverText("feedback_label", golden.label)}</td>
        <td data-label={t("golden.col_source")}>{serverText("golden_source", golden.source)}</td>
        <td data-label={t("golden.col_active")}>
          <input type="checkbox" className={styles.toggle} checked={golden.active} disabled={busy}
            aria-label={t("golden.active_for", { title: golden.title })}
            onChange={(event) => { flags.mutate({ caseId: golden.id, flags: { active: event.target.checked } }); }} />
        </td>
        <td data-label={t("golden.col_canary")}>
          <input type="checkbox" className={styles.toggle} checked={golden.is_canary} disabled={busy}
            aria-label={t("golden.canary_for", { title: golden.title })} aria-describedby="golden-canary-hint"
            onChange={(event) => { flags.mutate({ caseId: golden.id, flags: { is_canary: event.target.checked } }); }} />
        </td>
        <td>
          <button type="button" className={tables.action} disabled={busy} onClick={() => { setAsking(true); }}>
            {t("golden.delete")}<span className="visually-hidden"> {golden.title}</span>
          </button>
          <ConfirmDialog open={asking} title={t("golden.delete_title")} confirmLabel={t("golden.delete")}
            onCancel={() => { setAsking(false); }}
            onConfirm={() => {
              setAsking(false);
              remove.mutate(golden.id, { onSuccess: () => { toast({ message: t("golden.deleted") }); } });
            }}>
            <p>{t("golden.delete_body", { title: golden.title, company: golden.company })}</p>
          </ConfirmDialog>
        </td>
      </tr>
      {error !== null && (
        <tr className={tables.detail}>
          <td colSpan={COLUMNS} className={tables.wide}><ProblemBox error={error} /></td>
        </tr>
      )}
    </Fragment>
  );
}

export function GoldenSet() {
  const { t } = useTranslation();
  const cases = useGoldenCases();
  let body;
  if (cases.items === undefined) {
    body = cases.error === null ? <Skeleton variant="line" count={3} /> : <ProblemBox error={cases.error} onRetry={cases.retry} />;
  } else if (cases.items.length === 0) {
    body = <p className={parts.empty}>{t("golden.empty")}</p>;
  } else {
    body = (
      <>
        <div className={tables.scroll}>
          <table className={tables.table} aria-labelledby="golden-heading">
            <thead>
              <tr>
                <th scope="col">{t("golden.col_title")}</th>
                <th scope="col">{t("golden.col_company")}</th>
                <th scope="col">{t("golden.col_label")}</th>
                <th scope="col">{t("golden.col_source")}</th>
                <th scope="col">{t("golden.col_active")}</th>
                <th scope="col">{t("golden.col_canary")}</th>
                <th scope="col"><span className="visually-hidden">{t("golden.col_actions")}</span></th>
              </tr>
            </thead>
            <tbody>
              {cases.items.map((golden) => <CaseRow key={golden.id} golden={golden} />)}
            </tbody>
          </table>
        </div>
        <p id="golden-canary-hint" className={tables.note}>{t("golden.canary_hint")}</p>
        <LoadMore list={cases} />
      </>
    );
  }
  return (
    <section id="golden-set" aria-labelledby="golden-heading" className={parts.part}>
      <h3 id="golden-heading" className={parts.title}>{t("settings_system.golden")}</h3>
      {body}
    </section>
  );
}
