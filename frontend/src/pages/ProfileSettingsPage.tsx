// Profile & Settings (spec §15.7): a sticky navigation over the nine sections, each its own anchor. The later
// tasks fill each section's body.
import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import pages from "../app/Pages.module.css";
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

export function ProfileSettingsPage() {
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
          {SETTINGS_SECTIONS.map((id) => (
            <section key={id} id={id} aria-labelledby={`${id}-heading`} className={styles.section}>
              <h2 id={`${id}-heading`} className={styles.sectionTitle}>{t(`settings.section.${id}`)}</h2>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
