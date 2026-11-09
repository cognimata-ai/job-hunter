// Another job named in the drawer (spec §15.5, B26: the representative a deferred member is scored via, or whose
// description it shows): its title when the job's group lists it, else "job <id>", as a button that shows that
// job's detail in the drawer. Outside the drawer (System → Needs attention, plan M4 Task 31) the caller names the
// title it knows, if any, and its `onNavigate` opens the job in the Jobs page's drawer.
import { useTranslation } from "react-i18next";
import type { JobDetailOut } from "./detailApi";
import styles from "./JobRef.module.css";

export interface JobRefProps {
  jobId: number;
  /** The drawer's job, whose group may list this job's title. */
  detail?: JobDetailOut;
  /** The job's title, when the caller knows it. */
  title?: string;
  onNavigate: (jobId: number) => void;
}

export function JobRef({ jobId, detail, title, onNavigate }: JobRefProps) {
  const { t } = useTranslation();
  const name = title ?? detail?.group?.members.find((member) => member.job_id === jobId)?.title;
  return (
    <button type="button" className={styles.ref} onClick={() => { onNavigate(jobId); }}>
      {name ?? t("drawer.job_ref", { id: String(jobId) })}
    </button>
  );
}
