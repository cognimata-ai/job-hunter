// Profile & Settings (spec §15.7): a sticky navigation over the nine sections, each its own anchor. Each task
// fills its section's body: the CV & profile section is Task 27's (`settings/cv/CvSection`), first; the sections
// of the settings document share one form (`SettingsFormProvider`, Task 28, D32), each in a `SectionFrame` that
// ends with the dry-run panel and Save: Search scope, Schedule and Budget & quota are Task 28's, Queries & sources
// and Watchlist Task 29's, Scoring & models Task 30's, Privacy & data Task 31's. System (Task 31, Ruling T31-a)
// holds the framed Display & notifications part (`#display`), then its operational parts, which have no panel.
import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import pages from "../app/Pages.module.css";
import { BudgetSection } from "../settings/BudgetSection";
import { CvSection } from "../settings/cv/CvSection";
import { QueriesSection } from "../settings/QueriesSection";
import { ScheduleSection } from "../settings/ScheduleSection";
import { ScopeSection } from "../settings/ScopeSection";
import { ScoringSection } from "../settings/ScoringSection";
import { SectionFrame } from "../settings/SectionFrame";
import { SettingsFormProvider } from "../settings/useSettingsForm";
import { WatchlistSection } from "../settings/WatchlistSection";
import { PrivacySection } from "../system/PrivacySection";
import { SystemSection } from "../system/SystemSection";
import styles from "./ProfileSettingsPage.module.css";

export const SETTINGS_SECTIONS = [
  "cv",
  "scope",
  "queries",
  "watchlist",
  "schedule",
  "scoring",
  "budget",
  "system",
  "privacy",
] as const;

export type SettingsSection = (typeof SETTINGS_SECTIONS)[number];

/** The body of a section of the settings document, framed with the dry-run panel; `null` for a section that is
 * not one (CV & profile) or holds its own frames (System). */
function documentSection(id: SettingsSection): ReactNode {
  switch (id) {
    case "scope":
      return <ScopeSection />;
    case "queries":
      return <QueriesSection />;
    case "watchlist":
      return <WatchlistSection />;
    case "schedule":
      return <ScheduleSection />;
    case "scoring":
      return <ScoringSection />;
    case "budget":
      return <BudgetSection />;
    case "privacy":
      return <PrivacySection />;
    default:
      return null;
  }
}

export function ProfileSettingsPage() {
  return (
    <SettingsFormProvider>
      <SettingsPage />
    </SettingsFormProvider>
  );
}

function SettingsPage() {
  const { t } = useTranslation();
  return (
    <div className={styles.page}>
      <h1 className={pages.title}>{t("settings.title")}</h1>
      <div className={styles.layout}>
        <nav aria-label={t("settings.nav_label")} className={styles.nav}>
          <ul role="list" className={styles.navList}>
            {SETTINGS_SECTIONS.map((id) => (
              <li key={id}>
                <Link to="/settings" hash={id} activeOptions={{ includeHash: true }} className={styles.navLink}>
                  {t(`settings.section.${id}`)}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        <div className={styles.sections}>
          {SETTINGS_SECTIONS.map((id) => {
            const body = documentSection(id);
            if (body !== null) {
              return <SectionFrame key={id} id={id} title={t(`settings.section.${id}`)}>{body}</SectionFrame>;
            }
            return (
              <section key={id} id={id} aria-labelledby={`${id}-heading`} className={styles.section}>
                <h2 id={`${id}-heading`} className={styles.sectionTitle}>{t(`settings.section.${id}`)}</h2>
                {id === "cv" && <CvSection />}
                {id === "system" && <SystemSection />}
              </section>
            );
          })}
        </div>
      </div>
    </div>
  );
}
