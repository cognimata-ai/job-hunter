// Privacy & data (spec §20.1, §15.7; plan M4 Task 31, resolution f, U1): the retention table, what is kept and
// for how long, as static rows from the catalogue (§20.1's rows, the technical ones grouped), its one setting
// (`retention.score_history_months`, the model's 1 to 36) editable in its row; then the privacy statement's place
// (as text: the SPA serves no docs) and a note that export, the Adzuna purge and delete-everything come later.
import { useTranslation } from "react-i18next";
import { NumberField } from "../settings/fields";
import fields from "../settings/fields.module.css";
import styles from "./PrivacySection.module.css";

/** The rows of the retention table, in §20.1's order: `settings_privacy.row.<row>` and `….kept.<row>`. */
export const RETENTION_ROWS = [
  "cv_file",
  "stripped_text",
  "drafts",
  "profile",
  "proposals",
  "score_history",
  "feedback",
  "golden",
  "eval_runs",
  "raw_bodies",
  "raw_items",
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
      <p>{t("settings_privacy.statement")}</p>
      <p className={styles.later}>{t("settings_privacy.later")}</p>
    </div>
  );
}
