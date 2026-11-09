// The model grid of Scoring & models (spec §10.3, §15.7; plan M4 Task 30, resolution c): one row per node of
// `scoring.model_policy.nodes`, each with its model and its effort, then the critic's effort for a light round.
//
// - **The options.** The models are §10.3's (`presets.MODELS`, the schema's literal); a row's efforts are its
//   model's (`presets.effortsOf`, `codes.json`'s `order.effort`, derived from `llm/pricing.py`). Models and
//   efforts are API words, shown as their codes; the node names are the `llm_node` namespace's.
// - **A new model** keeps the row's effort when the model supports it, else the default effort (`effortFor`); the
//   critic's model does the same for the light effort. A stored effort the model lacks stays an option, so the
//   select shows the draft, and the dry-run's `unsupported` error marks it.
// - **The generator's effort** carries, beside its row, the copy of the generator's `low` (§10.3 L3897): the
//   `conflict` error of selecting it under a failed gate, else the warning `eval_failed` (a kept `low` under a
//   failed gate) or `eval_uncalibrated`, of the draft's dry-run while anything is dirty, else of the base's.
//   The select names that line and the panel's issues in `aria-describedby` (Task 28's error linking, through
//   `SelectField`'s `describedBy`).
// - Each cell is a `SelectField`: a model pick goes through its `onPick` (it writes the node's model with its
//   effort). On a wide screen a header row names the columns and each select's own label is visually hidden; on
//   a narrow one (and at 200 % zoom) each row is a card with its labels.
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { useServerText } from "../i18n/index";
import { SelectField } from "./fields";
import fields from "./fields.module.css";
import styles from "./ModelGrid.module.css";
import { type LlmNode, type Model, MODELS, NODES, effortFor, effortsOf } from "./presets";
import { useSettingsForm } from "./useSettingsForm";

const POLICY = "scoring.model_policy";
const GENERATOR_EFFORT = `${POLICY}.nodes.generate.effort`;
const LIGHT_EFFORT = `${POLICY}.critic_light_effort`;

/** A cell's options: a stored value they lack stays one, so the select shows the draft (the dry-run's error marks
 * it). */
function withValue<T extends string>(options: readonly T[], value: T): readonly T[] {
  return options.includes(value) ? options : [...options, value];
}

/** Models and efforts are API words: an option shows its code. */
const code = (value: string) => value;

/** The generator's line (module docstring), `null` without one. */
function useGeneratorNote(): { text: string; tone: "critical" | "warn" } | null {
  const { t } = useTranslation();
  const form = useSettingsForm();
  if (form.issues.some((issue) => issue.path === GENERATOR_EFFORT && issue.code === "conflict")) {
    return { text: t("settings_scoring.generator_low_conflict"), tone: "critical" };
  }
  const warnings = (form.dirty.size > 0 ? form.dryRun?.warnings : form.baseDryRun?.warnings) ?? [];
  const warning = warnings.find((w) => w.code === "eval_failed" || w.code === "eval_uncalibrated");
  if (warning === undefined) {
    return null;
  }
  return { text: t(warning.code === "eval_failed" ? "warning.eval_failed" : "warning.eval_uncalibrated"), tone: "warn" };
}

function NodeRow({ node }: { node: LlmNode }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const form = useSettingsForm();
  const nameId = useId();
  const noteId = useId();
  const policy = form.draft.scoring.model_policy;
  const choice = policy.nodes[node];
  const path = `${POLICY}.nodes.${node}`;
  const note = useGeneratorNote();
  const shown = node === "generate" ? note : null;
  const pickModel = (model: Model) => {
    form.set(path, { model, effort: effortFor(model, choice.effort) });
    if (node === "critic") {
      const light = effortFor(model, policy.critic_light_effort);
      if (light !== policy.critic_light_effort) {
        form.set(LIGHT_EFFORT, light);
      }
    }
  };
  return (
    <div role="group" aria-labelledby={nameId} className={styles.row} data-tone={shown?.tone}>
      <span id={nameId} className={styles.node}>{serverText("llm_node", node)}</span>
      <SelectField path={`${path}.model`} label={t("settings_scoring.model")} options={withValue(MODELS, choice.model)}
        optionLabel={code} onPick={pickModel} />
      <SelectField path={`${path}.effort`} label={t("settings_scoring.effort")}
        options={withValue(effortsOf(choice.model), choice.effort)} optionLabel={code}
        describedBy={shown === null ? undefined : noteId} />
      {shown !== null && <p id={noteId} className={cx(styles.note, shown.tone === "critical" && styles.critical)}>{shown.text}</p>}
    </div>
  );
}

function LightRow() {
  const { t } = useTranslation();
  const form = useSettingsForm();
  const nameId = useId();
  const policy = form.draft.scoring.model_policy;
  const critic = policy.nodes.critic.model;
  return (
    <div role="group" aria-labelledby={nameId} className={styles.row}>
      <span id={nameId} className={styles.node}>{t("settings_scoring.critic_light")}</span>
      <span className={styles.same}>{t("settings_scoring.critic_light_model", { model: critic })}</span>
      <SelectField path={LIGHT_EFFORT} label={t("settings_scoring.effort")}
        options={withValue(effortsOf(critic), policy.critic_light_effort)} optionLabel={code} />
    </div>
  );
}

export function ModelGrid() {
  const { t } = useTranslation();
  const headingId = useId();
  return (
    <div role="group" aria-labelledby={headingId} className={styles.grid}>
      <h3 id={headingId} className={fields.heading}>{t("settings_scoring.models")}</h3>
      <div className={styles.head} aria-hidden="true">
        <span>{t("settings_scoring.node")}</span>
        <span>{t("settings_scoring.model")}</span>
        <span>{t("settings_scoring.effort")}</span>
      </div>
      {NODES.map((node) => <NodeRow key={node} node={node} />)}
      <LightRow />
    </div>
  );
}
