// The Duplicates tab (spec §15.4, §15.5: "The drawer shows a per-member breakdown"; plan M4 Task 25, resolution
// d): the members of the job's duplicate group, the representative and the opened job marked, with the group
// card's actions (`GroupMembers`); another member's title shows that member in the drawer. A job in no group
// (or in one just dissolved) says so. After Confirm, a member other than the representative is merged: its next
// read is a 404 with its survivor, which the drawer shows instead (B28).
import { useTranslation } from "react-i18next";
import { EmptyState } from "../components/EmptyState";
import type { JobDetailOut } from "./detailApi";
import styles from "./DuplicatesTab.module.css";
import { GroupMembers } from "./GroupMembers";

export interface DuplicatesTabProps {
  detail: JobDetailOut;
  onNavigate: (jobId: number) => void;
}

export function DuplicatesTab({ detail, onNavigate }: DuplicatesTabProps) {
  const { t } = useTranslation();
  const { group } = detail;
  if (group === null || group.status === "dissolved") {
    return <EmptyState title={t("group.none")} />;
  }
  return (
    <div className={styles.tab}>
      <GroupMembers group={group} thisJobId={detail.job.job_id} onOpen={onNavigate} />
    </div>
  );
}
