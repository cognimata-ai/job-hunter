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
//   The select names that line and the panel's issues in `aria-describedby` (Task 28's error linking).
// - On a wide screen a header row names the columns and each select's own label is visually hidden; on a narrow
//   one (and at 200 % zoom) each row is a card with its labels.
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { useServerText } from "../i18n/index";
import { describedBy, useFieldIssues } from "./fields";
import fields from "./fields.module.css";
import styles from "./ModelGrid.module.css";
import { type Effort, type LlmNode, type Model, MODELS, NODES, effortFor, effortsOf } from "./presets";
import { fieldId, useSettingsForm } from "./useSettingsForm";

const POLICY = "scoring.model_policy";
const GENERATOR_EFFORT = `${POLICY}.nodes.generate.effort`;
const LIGHT_EFFORT = `${POLICY}.critic_light_effort`;

/** A native select of a grid cell: its label, its issues (Task 28's linking) and what a pick writes. */
function GridSelect<T extends string>({
  path,
  label,
  options,
  value,
  onPick,
  note,
}: {
  path: string;
  label: string;
  options: readonly T[];
  value: T;
  onPick: (value: T) => void;
  /** The id of a line about the cell (the generator's `low`). */
  note?: string;
}) {
  const id = fieldId(path);
  const { invalid, issueIds } = useFieldIssues(path);
  // a stored value the options lack stays shown (the dry-run's error marks it)
  const shown = options.includes(value) ? options : [...options, value];
  return (
    <div className={fields.field}>
      <label htmlFor={id} className={cx(fields.label, styles.cellLabel)}>{label}</label>
      <select id={id} className={fields.select} value={value} aria-invalid={invalid || undefined}
        aria-describedby={describedBy(issueIds, note)}
        onChange={(event) => {
          const picked = shown.find((option) => option === event.target.value);
          if (picked !== undefined) {
            onPick(picked);
          }
        }}>
        {shown.map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
    </div>
  );
}

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
      <GridSelect path={`${path}.model`} label={t("settings_scoring.model")} options={MODELS} value={choice.model}
        onPick={pickModel} />
      <GridSelect path={`${path}.effort`} label={t("settings_scoring.effort")} options={effortsOf(choice.model)}
        value={choice.effort} onPick={(effort: Effort) => { form.set(`${path}.effort`, effort); }}
        note={shown === null ? undefined : noteId} />
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
      <GridSelect path={LIGHT_EFFORT} label={t("settings_scoring.effort")} options={effortsOf(critic)}
        value={policy.critic_light_effort} onPick={(effort: Effort) => { form.set(LIGHT_EFFORT, effort); }} />
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
