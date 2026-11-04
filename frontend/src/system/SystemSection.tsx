// System on Profile & Settings (spec §15.7; plan M4 Task 31, Ruling T31-a): Display & notifications first, a
// settings part framed with its own dry-run panel and Save under the anchor `#display` (D31 puts it in System);
// then the operational parts, which hold no settings fields and so no panel: Needs attention, Runs, Score cycles,
// Alerts, the golden set and Health, each under an anchor of its own.
import { useTranslation } from "react-i18next";
import { SectionFrame } from "../settings/SectionFrame";
import { AlertsList } from "./AlertsList";
import { CyclesList } from "./CyclesList";
import { DisplaySection } from "./DisplaySection";
import { GoldenSet } from "./GoldenSet";
import { HealthPanel } from "./HealthPanel";
import { NeedsAttention } from "./NeedsAttention";
import { RunsList } from "./RunsList";
import styles from "./SystemSection.module.css";

export function SystemSection() {
  const { t } = useTranslation();
  return (
    <div className={styles.system}>
      <SectionFrame id="system" anchor="display" level={3} title={t("settings_display.title")}>
        <DisplaySection />
      </SectionFrame>
      <NeedsAttention />
      <RunsList />
      <CyclesList />
      <AlertsList />
      <GoldenSet />
      <HealthPanel />
    </div>
  );
}
