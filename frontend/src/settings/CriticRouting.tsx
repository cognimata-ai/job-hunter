// Critic routing of Scoring & models (spec §11.7.4, §15.7; plan M4 Task 30, resolution c): the evaluation
// policy's fields (the relevance threshold, the T2 band and straddle coverage, T3's threshold, T8's sample and
// the triggers reviewed at the light effort), then the critic rate:
// - **projected**: the dry-run's `critic_rate` projection (a draft that changes the evaluation policy), else the
//   active evaluation policy's, `GET /api/system/spend`'s `projected_critic_rate`;
// - **measured (7 days)**: the spend read's `critic_rate_7d`, "—" while it is null (below 50 attempts).
// Rates are `Intl.NumberFormat` percentages. The spend is read as Budget & quota reads it (`useSpend` live: the
// one query on this page, fresh for 60 s, polled in the background). No field writes out a bound.
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { intlLocale, useServerText } from "../i18n/index";
import { useSpend } from "../jobs/api";
import styles from "./CriticRouting.module.css";
import { CheckboxSetField, NumberField, closedSet } from "./fields";
import fields from "./fields.module.css";
import { type Settings, useSettingsForm } from "./useSettingsForm";

type LightTrigger = Settings["evaluation"]["critic_light_triggers"][number];

const LIGHT_TRIGGERS = closedSet<LightTrigger>({ T2: true, T3: true, T8: true });

/** A rate (a decimal string) as a percentage, "—" for none. */
function useRate(): (rate: string | null | undefined) => string {
  const { t, i18n } = useTranslation();
  const percent = new Intl.NumberFormat(intlLocale(i18n.language), { style: "percent", maximumFractionDigits: 1 });
  return (rate) => (rate == null ? t("settings_scoring.rate_none") : percent.format(Number(rate)));
}

export function CriticRouting() {
  const { t } = useTranslation();
  const serverText = useServerText();
  const form = useSettingsForm();
  const spend = useSpend({ live: true });
  const rate = useRate();
  const headingId = useId();
  const projected = form.dryRun?.projections?.critic_rate ?? spend.data?.projected_critic_rate;
  const percent = t("settings_scoring.unit_percent");
  return (
    <div role="group" aria-labelledby={headingId} className={styles.routing}>
      <h3 id={headingId} className={fields.heading}>{t("settings_scoring.routing")}</h3>
      <div className={fields.grid}>
        <NumberField path="evaluation.relevance_threshold" label={t("settings_scoring.relevance_threshold")} />
        <NumberField path="evaluation.t2_band" label={t("settings_scoring.t2_band")} unit={t("settings_scoring.unit_points")}
          hint={t("settings_scoring.t2_band_hint")} />
        <NumberField path="evaluation.t2_straddle_max_coverage" label={t("settings_scoring.t2_coverage")} decimal
          hint={t("settings_scoring.t2_coverage_hint")} />
        <NumberField path="evaluation.t3_min_mid" label={t("settings_scoring.t3_min_mid")} />
        <NumberField path="evaluation.t8_sample_pct" label={t("settings_scoring.t8_sample_pct")} unit={percent} />
      </div>
      <CheckboxSetField path="evaluation.critic_light_triggers" label={t("settings_scoring.light_triggers")}
        options={LIGHT_TRIGGERS} optionLabel={(trigger) => serverText("critic_trigger", trigger)} />
      <dl className={styles.figures} aria-label={t("settings_scoring.rates")}>
        <div className={styles.figure}>
          <dt>{t("settings_scoring.projected_rate")}</dt>
          <dd>{rate(projected)}</dd>
        </div>
        <div className={styles.figure}>
          <dt>{t("settings_scoring.measured_rate")}</dt>
          <dd>{rate(spend.data?.critic_rate_7d)}</dd>
        </div>
      </dl>
    </div>
  );
}
