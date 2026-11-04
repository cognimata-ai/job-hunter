// The eval status of Scoring & models (spec §11.13, §15.7; plan M4 Task 30, resolution c), read from
// `GET /api/eval-runs?kind=golden` (newest first; the newest run alone is asked for):
// - "Last eval: passed, ρ = 0.71": the newest golden run's gate (passed, failed or not calibrated) once it has
//   completed and been gated, else its status (running, completed but not gated, did not finish), and ρ to two
//   decimals when it has one; "No golden run yet" without a run;
// - the eval gate: the active configuration's (the read's `active_gate`, with the golden cases it counts), and
//   while the draft changes a gated class, the gate its save would check (the dry-run's `eval_gate`);
// - that golden runs start from the command line (RUNBOOK): the page only reads them.
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { type ReactNode, useId } from "react";
import { useTranslation } from "react-i18next";
import { apiSend } from "../api/client";
import type { components } from "../api/schema";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { intlLocale, useServerText } from "../i18n/index";
import { useFormat } from "../system/format";
import routing from "./CriticRouting.module.css";
import styles from "./EvalStatus.module.css";
import fields from "./fields.module.css";
import { useSettingsForm } from "./useSettingsForm";

type EvalRunsPageOut = components["schemas"]["EvalRunsPageOut"];
type EvalRunOut = components["schemas"]["EvalRunOut"];
type ServerText = ReturnType<typeof useServerText>;

export const GOLDEN_RUNS_KEY = ["eval-runs", "golden"] as const;

function useGoldenRuns(): UseQueryResult<EvalRunsPageOut> {
  return useQuery({
    queryKey: GOLDEN_RUNS_KEY,
    queryFn: ({ signal }) => apiSend<EvalRunsPageOut>("/api/eval-runs?kind=golden&limit=1", { signal }),
  });
}

/** An eval's result: its state, with ρ to two decimals in the UI language when it has one (`passed, ρ = 0.71`). */
export function useEvalResult(): (state: string, rho: string | null) => string {
  const { t, i18n } = useTranslation();
  const decimals = new Intl.NumberFormat(intlLocale(i18n.language), { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (state, rho) => (rho === null ? state : t("settings_scoring.eval_result_rho", { state, rho: decimals.format(Number(rho)) }));
}

/** A golden run's state as the last eval shows it (module docstring). */
function runState(run: EvalRunOut, serverText: ServerText): string {
  if (run.status === "completed" && run.gate !== null) {
    return serverText("eval_gate", run.gate);
  }
  return serverText("eval_run_status", run.status);
}

function lastEval(run: EvalRunOut | undefined, t: TFunction, serverText: ServerText, result: ReturnType<typeof useEvalResult>): string {
  if (run === undefined) {
    return t("settings_scoring.no_golden_run");
  }
  return t("settings_scoring.last_eval", { outcome: result(runState(run, serverText), run.spearman_rho) });
}

export function EvalStatus() {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const form = useSettingsForm();
  const runs = useGoldenRuns();
  const result = useEvalResult();
  const headingId = useId();
  const draftGate = form.dryRun?.projections?.eval_gate;
  let body: ReactNode;
  if (runs.isPending) {
    body = <Skeleton variant="line" count={2} />;
  } else if (runs.isError) {
    body = <ProblemBox error={runs.error} onRetry={() => { void runs.refetch(); }} />;
  } else {
    const gate = runs.data.active_gate;
    body = (
      <>
        <p className={styles.last}>{lastEval(runs.data.items.at(0), t, serverText, result)}</p>
        <dl className={routing.figures}>
          <div className={routing.figure}>
            <dt>{t("settings_scoring.gate")}</dt>
            <dd>
              {t("settings_scoring.gate_value", { count: gate.cases, state: serverText("eval_gate", gate.state), value: format.number(gate.cases) })}
            </dd>
          </div>
          {draftGate != null && (
            <div className={routing.figure}>
              <dt>{t("settings_scoring.gate_draft")}</dt>
              <dd>{serverText("eval_gate", draftGate)}</dd>
            </div>
          )}
        </dl>
      </>
    );
  }
  return (
    <div role="group" aria-labelledby={headingId} className={styles.eval}>
      <h3 id={headingId} className={fields.heading}>{t("settings_scoring.eval")}</h3>
      {body}
      <p className={fields.hint}>{t("settings_scoring.golden_cli")}</p>
    </div>
  );
}
