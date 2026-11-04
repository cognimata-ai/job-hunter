// A section of the settings document on Profile & Settings (spec §15.7; plan M4 Task 28, Ruling T28-b): its
// anchor, its heading, its body and, at its end, the dry-run panel with Save. The body and the panel need the
// form, so until the settings are read the frame shows a skeleton, or their failure with Retry: one alert for the
// page, in the first framed section, and the same text quietly in the others.
import { type ReactNode, useEffect } from "react";
import { Skeleton } from "../components/Skeleton";
import type { SettingsSection } from "../pages/ProfileSettingsPage";
import { DryRunPanel, Problem } from "./DryRunPanel";
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
  const failed = form === null && load !== null && load.error !== null;
  const frame = load?.frame;
  useEffect(() => (failed && frame !== undefined ? frame(id) : undefined), [failed, frame, id]);
  let body: ReactNode;
  if (form !== null) {
    body = (
      <SectionProvider section={id}>
        <div className={styles.body}>{children}</div>
        <DryRunPanel />
      </SectionProvider>
    );
  } else if (load !== null && load.error !== null) {
    body = <Problem error={load.error} alert={load.alertFor === id} onRetry={load.retry} />;
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
