// A section of the settings document on Profile & Settings (spec §15.7; plan M4 Task 28, Ruling T28-b): its
// anchor, its heading, its body and, at its end, the dry-run panel with Save. The body and the panel need the
// form, so until the settings are read the frame shows a skeleton, or their failure with Retry.
import type { ReactNode } from "react";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import type { SettingsSection } from "../pages/ProfileSettingsPage";
import { DryRunPanel } from "./DryRunPanel";
import styles from "./SectionFrame.module.css";
import { SectionProvider, useSettingsFormOrNull, useSettingsLoad } from "./useSettingsForm";

export interface SectionFrameProps {
  /** The section's anchor (`#scope`). */
  id: SettingsSection;
  /** The section's heading, translated. */
  title: string;
  children?: ReactNode;
}

export function SectionFrame({ id, title, children }: SectionFrameProps) {
  const form = useSettingsFormOrNull();
  const load = useSettingsLoad();
  let body: ReactNode;
  if (form !== null) {
    body = (
      <SectionProvider section={id}>
        <div className={styles.body}>{children}</div>
        <DryRunPanel />
      </SectionProvider>
    );
  } else if (load !== null && load.error !== null) {
    body = <ProblemBox error={load.error} onRetry={load.retry} />;
  } else {
    body = <Skeleton variant="line" count={3} />;
  }
  return (
    <section id={id} aria-labelledby={`${id}-heading`} className={styles.section}>
      <h2 id={`${id}-heading`} className={styles.title}>{title}</h2>
      {body}
    </section>
  );
}
