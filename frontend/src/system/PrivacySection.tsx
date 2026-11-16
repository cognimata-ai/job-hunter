// Privacy & data (spec §15.7, §20.1; plan M4 Task 31, resolution f, Ruling T31-c; plan M5 Task 20), in §15.7's
// order: the actions (export, the Adzuna purge, delete everything: `PrivacyActions`), each deletion's status
// (`DeletionStatus`), then the retention table, what is kept and for how long, as static rows from the catalogue
// (§20.1's rows, four pairs merged), its one setting (`retention.score_history_months`, the model's 1 to 36)
// editable in its row; the backup lag note (`settings_privacy.later`), and a link to the privacy statement, which
// the page renders under its sections (`PrivacyStatement`, `#privacy-statement`).
import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { NumberField } from "../settings/fields";
import fields from "../settings/fields.module.css";
import { DeletionStatus } from "./DeletionStatus";
import { PrivacyActions } from "./PrivacyActions";
import styles from "./PrivacySection.module.css";
import { STATEMENT_ID } from "./PrivacyStatement";

/**
 * The rows of the retention table, in §20.1's order: `settings_privacy.row.<row>` (what is kept) and
 * `….kept.<row>` (for how long). Each states §20.1's rows exactly, a merged one each merged rule's period and
 * what it keeps (Ruling T31-c); `tests/unit/test_privacy_retention_rows.py` maps every §20.1 row to one of these
 * and checks its periods in both languages. Merged: `proposals` (query proposals, profile vectors), `ledgers`
 * (the ledgers and windows, the scoring configurations), `finished_jobs` (finished procrastinate jobs,
 * idempotency records), `sessions` (sessions, login attempts).
 */
export const RETENTION_ROWS = [
  "cv_file",
  "stripped_text",
  "drafts",
  "profile",
  "proposals",
  "score_history",
  "det_superseded",
  "feedback",
  "golden",
  "eval_runs",
  "raw_bodies",
  "raw_items",
  "raw_responses",
  "run_metadata",
  "batch_submissions",
  "ats_snapshots",
  "ledgers",
  "jobs",
  "closed_jobs",
  "saved_jobs",
  "embeddings",
  "checkpoints",
  "finished_jobs",
  "logs",
  "sessions",
  "alerts",
  "exports",
  "backups",
  "backup_records",
  "manifests",
  "traces",
] as const;

type Row = (typeof RETENTION_ROWS)[number];

/** The row whose retention is a setting. */
const EDITABLE: Row = "score_history";

// `settings/model.py` `Retention.score_history_months`: ge=1, le=36.
const MIN_MONTHS = 1;
const MAX_MONTHS = 36;

export function PrivacySection() {
  const { t } = useTranslation();
  return (
    <div className={styles.privacy}>
      <PrivacyActions />
      <DeletionStatus />
      <div className={styles.retention}>
        <p className={styles.intro}>{t("settings_privacy.intro")}</p>
        <div className={styles.scroll}>
          <table className={styles.table}>
            <caption className={styles.caption}>{t("settings_privacy.table")}</caption>
            <thead>
              <tr>
                <th scope="col">{t("settings_privacy.col_data")}</th>
                <th scope="col">{t("settings_privacy.col_kept")}</th>
              </tr>
            </thead>
            <tbody>
              {RETENTION_ROWS.map((row) => (
                <tr key={row} className={row === EDITABLE ? styles.editable : undefined}>
                  <th scope="row" className={styles.what}>{t(`settings_privacy.row.${row}`)}</th>
                  <td>
                    {row === EDITABLE ? (
                      <div className={styles.kept}>
                        <NumberField path="retention.score_history_months" label={t("settings_privacy.score_history_months")}
                          min={MIN_MONTHS} max={MAX_MONTHS} unit={t("settings_privacy.months")} />
                        <span className={fields.hint}>{t(`settings_privacy.kept.${row}`)}</span>
                      </div>
                    ) : t(`settings_privacy.kept.${row}`)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className={styles.later}>{t("settings_privacy.later")}</p>
        <p className={styles.statement}>
          <Link to="/settings" hash={STATEMENT_ID}>{t("settings_privacy.statement")}</Link>
        </p>
      </div>
    </div>
  );
}
