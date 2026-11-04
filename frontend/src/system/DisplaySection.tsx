// System → Display & notifications (spec §13.1, §15.7; plan M4 Task 31, resolution c, D31): the settings part at
// the top of System, framed with its own dry-run panel and Save (Ruling T31-a). Its fields are the document's
// `display.*` and `notifications.*`:
// - **Time zone**: a select of `Intl.supportedValuesOf("timeZone")`, plus the saved and the draft value when the
//   list lacks it (an alias, `UTC`); the server validates the zone, and its refusal comes through the panel.
// - **UI language**: English or German. After the save, the shell's read of the settings (`AppShell`,
//   `SETTINGS_KEY`) switches the UI's language.
// - **Notification threshold** (`notifications.push_min_severity`): warnings and up, or critical alerts only.
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useServerText } from "../i18n/index";
import { closedSet, SelectField } from "../settings/fields";
import fields from "../settings/fields.module.css";
import { type Settings, useSettingsForm } from "../settings/useSettingsForm";

type UiLanguage = Settings["display"]["ui_language"];
type Threshold = Settings["notifications"]["push_min_severity"];

const LANGUAGES = closedSet<UiLanguage>({ en: true, de: true });
const THRESHOLDS = closedSet<Threshold>({ warn: true, critical: true });

export function DisplaySection() {
  const { t } = useTranslation();
  const serverText = useServerText();
  const form = useSettingsForm();
  const saved = form.base.payload.display.timezone;
  const draft = form.draft.display.timezone;
  const zones = useMemo(() => {
    const supported = Intl.supportedValuesOf("timeZone");
    const missing = [saved, draft].filter((zone, i, all) => zone !== "" && !supported.includes(zone) && all.indexOf(zone) === i);
    return missing.length === 0 ? supported : [...supported, ...missing].sort();
  }, [saved, draft]);
  return (
    <div className={fields.grid}>
      <SelectField path="display.timezone" label={t("settings_display.timezone")} hint={t("settings_display.timezone_hint")}
        options={zones} optionLabel={(zone) => zone} />
      <SelectField path="display.ui_language" label={t("settings_display.language")} hint={t("settings_display.language_hint")}
        options={LANGUAGES} optionLabel={(language) => serverText("ui_language", language)} />
      <SelectField path="notifications.push_min_severity" label={t("settings_display.threshold")} hint={t("settings_display.threshold_hint")}
        options={THRESHOLDS} optionLabel={(severity) => serverText("alert_severity", severity)} />
    </div>
  );
}
