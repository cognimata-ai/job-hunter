// The execution grid of Scoring & models (spec §10.3, §15.7; plan M4 Task 30, resolution c): per node of
// `scoring.execution`, the first stage's `max_tokens`, the enlarged stage's `max_tokens_retry` and the node
// deadline `deadline_s`, as number fields. No field writes out a bound: the dry-run's errors are the check
// (`max_tokens_retry ≥ max_tokens` included). A change is the `execution` effect, which never re-scores. The rows
// have the model grid's layout (`ModelGrid.module.css`), with three cells.
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { useServerText } from "../i18n/index";
import styles from "./ExecutionGrid.module.css";
import { NumberField } from "./fields";
import fields from "./fields.module.css";
import grid from "./ModelGrid.module.css";
import { type LlmNode, NODES } from "./presets";

function StageRow({ node }: { node: LlmNode }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const nameId = useId();
  const path = `scoring.execution.${node}`;
  const tokens = t("settings_scoring.unit_tokens");
  return (
    <div role="group" aria-labelledby={nameId} className={cx(grid.row, styles.cells)}>
      <span id={nameId} className={grid.node}>{serverText("llm_node", node)}</span>
      <NumberField path={`${path}.max_tokens`} label={t("settings_scoring.max_tokens")} unit={tokens} />
      <NumberField path={`${path}.max_tokens_retry`} label={t("settings_scoring.max_tokens_retry")} unit={tokens} />
      <NumberField path={`${path}.deadline_s`} label={t("settings_scoring.deadline_s")} unit={t("settings_scoring.unit_seconds")} />
    </div>
  );
}

export function ExecutionGrid() {
  const { t } = useTranslation();
  const headingId = useId();
  const hintId = useId();
  return (
    <div role="group" aria-labelledby={headingId} aria-describedby={hintId} className={grid.grid}>
      <h3 id={headingId} className={fields.heading}>{t("settings_scoring.execution")}</h3>
      <p id={hintId} className={fields.hint}>{t("settings_scoring.execution_hint")}</p>
      <div className={cx(grid.head, styles.cells)} aria-hidden="true">
        <span>{t("settings_scoring.node")}</span>
        <span>{t("settings_scoring.max_tokens")}</span>
        <span>{t("settings_scoring.max_tokens_retry")}</span>
        <span>{t("settings_scoring.deadline_s")}</span>
      </div>
      {NODES.map((node) => <StageRow key={node} node={node} />)}
    </div>
  );
}
