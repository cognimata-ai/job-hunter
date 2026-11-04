// Schedule (spec §3.4, §15.7; plan M4 Task 28, resolution f): enabled, the interval (1 to 168 hours), the anchor
// time (`HH:MM`, local to the display time zone the document holds) and whether scheduled runs include the paid
// sources; then the next slots: the saved schedule's three (the header status's `next_slots`) and, with a
// schedule change, the draft's next one (the dry-run's `next_slot`; none while the draft's schedule is off).
// Every time is shown in the display time zone.
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useStatus } from "../system/api";
import { useFormat } from "../system/format";
import { NumberField, TextField, ToggleField } from "./fields";
import fields from "./fields.module.css";
import styles from "./ScheduleSection.module.css";
import { useSettingsForm } from "./useSettingsForm";

export function ScheduleSection() {
  const { t } = useTranslation();
  const form = useSettingsForm();
  const format = useFormat();
  const status = useStatus();
  const next = form.dirty.size > 0 ? form.dryRun?.projections?.next_slot : undefined;
  let saved: ReactNode;
  if (status.isPending) {
    saved = <Skeleton variant="line" count={1} />;
  } else if (status.isError) {
    saved = <ProblemBox error={status.error} onRetry={() => { void status.refetch(); }} />;
  } else if (status.data.next_slots.length === 0) {
    saved = <p>{t("settings_schedule.next_none")}</p>;
  } else {
    saved = (
      <ul role="list" className={styles.slots}>
        {status.data.next_slots.map((slot) => <li key={slot}>{format.dayMonthTime(slot)}</li>)}
      </ul>
    );
  }
  return (
    <div className={styles.schedule}>
      <div className={fields.grid}>
        <ToggleField path="schedule.enabled" label={t("settings_schedule.enabled")} />
        <NumberField path="schedule.interval_hours" label={t("settings_schedule.interval")} min={1} max={168}
          unit={t("settings_schedule.hours")} />
        <TextField path="schedule.anchor_local" type="time" label={t("settings_schedule.anchor")}
          hint={t("settings_schedule.anchor_hint", { timezone: form.draft.display.timezone })} />
        <ToggleField path="schedule.include_paid" label={t("settings_schedule.include_paid")} />
      </div>
      <div className={styles.next}>
        <div className={styles.nextGroup}>
          <h3 className={fields.heading}>{t("settings_schedule.next_saved")}</h3>
          {saved}
        </div>
        {next !== undefined && (
          <div className={styles.nextGroup}>
            <h3 className={fields.heading}>{t("settings_schedule.next_draft")}</h3>
            <p>{next === null ? t("settings_schedule.next_none") : format.dayMonthTime(next)}</p>
          </div>
        )}
      </div>
    </div>
  );
}
