// Scoring & models (spec §10.3, §11.7.4, §11.13, §13.3, §15.7; plan M4 Task 30, resolutions b to e): the model
// preset, the model grid, the execution grid, critic routing with its rates, the monthly cost projection, the
// eval status, and the shipped rubric and prompt/schema versions.
//
// - **The preset selector** is a radio group of A, B, C and Custom. Choosing A, B or C writes that preset's grid
//   and its critic light effort into the draft (`presets.PRESETS`). The option checked is the label of the
//   draft's grid (`labelFor`, D20), so editing any cell relabels; Custom is never chosen by hand, and is disabled
//   while the grid is a preset's. The draft's own `preset` is left as it is: the server stores the label of the
//   grid whatever a request carries, so a label alone is no change.
// - **The monthly cost projection** is the dry-run's `llm` projection (the draft's while anything is dirty, else
//   the base document's): the month's projected spend, and the ads a month the cap admits at `ĉ`.
// - A save whose effects include `llm_tuple` asks first, with the re-score's cost: the dry-run panel's Save
//   (`DryRunPanel`), shared by every section.
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { intlLocale, useServerText } from "../i18n/index";
import { useFormat } from "../system/format";
import { CriticRouting } from "./CriticRouting";
import routing from "./CriticRouting.module.css";
import { usdFine } from "./DryRunPanel";
import { EvalStatus } from "./EvalStatus";
import { ExecutionGrid } from "./ExecutionGrid";
import fields from "./fields.module.css";
import { ModelGrid } from "./ModelGrid";
import { POLICY_LABELS, PRESETS, type PresetLabel, labelFor } from "./presets";
import styles from "./ScoringSection.module.css";
import { useSettingsForm } from "./useSettingsForm";
import { VersionsPanel } from "./VersionsPanel";

function PresetSelector() {
  const { t } = useTranslation();
  const serverText = useServerText();
  const form = useSettingsForm();
  const name = useId();
  const legendId = useId();
  const hintId = useId();
  const label = labelFor(form.draft.scoring.model_policy);
  const choose = (preset: PresetLabel) => {
    form.set("scoring.model_policy.nodes", PRESETS[preset].nodes);
    form.set("scoring.model_policy.critic_light_effort", PRESETS[preset].critic_light_effort);
  };
  return (
    <div role="radiogroup" aria-labelledby={legendId} aria-describedby={hintId} className={styles.presets}>
      <h3 id={legendId} className={fields.heading}>{t("settings_scoring.presets")}</h3>
      <div className={styles.options}>
        {POLICY_LABELS.map((option) => (
          <label key={option} className={styles.option}>
            <input type="radio" name={name} value={option} checked={label === option}
              disabled={option === "custom" && label !== "custom"}
              onChange={() => {
                if (option !== "custom") {
                  choose(option);
                }
              }} />
            <span>{serverText("model_preset", option)}</span>
          </label>
        ))}
      </div>
      <p id={hintId} className={fields.hint}>{t("settings_scoring.custom_hint")}</p>
    </div>
  );
}

function MonthlyCost() {
  const { t, i18n } = useTranslation();
  const format = useFormat();
  const form = useSettingsForm();
  const fine = usdFine(intlLocale(i18n.language));
  const projections = form.dirty.size > 0 ? form.dryRun?.projections : form.baseDryRun?.projections;
  const llm = projections?.llm;
  return (
    <dl className={routing.figures}>
      <div className={routing.figure}>
        <dt>{t("settings_scoring.cost")}</dt>
        <dd>
          {llm === undefined
            ? t("settings_scoring.rate_none")
            : t("settings_scoring.cost_value", {
                count: llm.targets_per_month,
                projected: format.usd(llm.projected_month_spend),
                targets: format.number(llm.targets_per_month),
                c_hat: fine(llm.c_hat),
              })}
        </dd>
      </div>
    </dl>
  );
}

export function ScoringSection() {
  return (
    <div className={styles.scoring}>
      <PresetSelector />
      <ModelGrid />
      <ExecutionGrid />
      <CriticRouting />
      <MonthlyCost />
      <EvalStatus />
      <VersionsPanel />
    </div>
  );
}
