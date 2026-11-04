// Another job named in the drawer (spec §15.5, B26: the representative a deferred member is scored via, or whose
// description it shows): its title when the job's group lists it, else "job <id>", as a button that shows that
// job's detail in the drawer.
import { useTranslation } from "react-i18next";
import type { JobDetailOut } from "./detailApi";
import styles from "./JobRef.module.css";

export interface JobRefProps {
  jobId: number;
  detail: JobDetailOut;
  onNavigate: (jobId: number) => void;
}

export function JobRef({ jobId, detail, onNavigate }: JobRefProps) {
  const { t } = useTranslation();
  const title = detail.group?.members.find((member) => member.job_id === jobId)?.title;
  return (
    <button type="button" className={styles.ref} onClick={() => { onNavigate(jobId); }}>
      {title ?? t("drawer.job_ref", { id: String(jobId) })}
    </button>
  );
}
