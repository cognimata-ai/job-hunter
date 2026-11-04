// A section of the settings document on Profile & Settings (spec §15.7; plan M4 Task 28, Ruling T28-b): its
// anchor, its heading, its body and, at its end, the dry-run panel with Save. The body and the panel need the
// form, so until the settings are read the frame shows a skeleton, or their failure with Retry: one alert for the
// page, in the first framed section, and the same text quietly in the others. A part of a page section (Display &
// notifications inside System, plan M4 Task 31, Ruling T31-a) has an anchor of its own (`anchor`) and a heading a
// level down (`level`); its fields' issues are still its section's (`id`).
import { type ReactNode, useEffect } from "react";
import { cx } from "../components/cx";
import { Skeleton } from "../components/Skeleton";
import type { SettingsSection } from "../pages/ProfileSettingsPage";
import { DryRunPanel, Problem } from "./DryRunPanel";
import styles from "./SectionFrame.module.css";
import { SectionProvider, useSettingsFormOrNull, useSettingsLoad } from "./useSettingsForm";

export interface SectionFrameProps {
  /** The section (its fields' issues, its panel), and its anchor (`#scope`) unless `anchor` names another. */
  id: SettingsSection;
  /** The section's heading, translated. */
  title: string;
  /** The anchor of a part of a section (`#display`, inside `#system`); `id` by default. */
  anchor?: string;
  /** The heading's level: 2 for a section of the page, 3 for a part of one. */
  level?: 2 | 3;
  children?: ReactNode;
}

export function SectionFrame({ id, title, anchor = id, level = 2, children }: SectionFrameProps) {
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
  const Heading = level === 2 ? "h2" : "h3";
  return (
    <section id={anchor} aria-labelledby={`${anchor}-heading`} className={cx(styles.section, level === 3 && styles.part)}>
      <Heading id={`${anchor}-heading`} className={cx(styles.title, level === 3 && styles.partTitle)}>{title}</Heading>
      {body}
    </section>
  );
}
