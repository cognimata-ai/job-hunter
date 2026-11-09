// The activation of a shipped rubric or prompt/schema version (spec §13.3 L4916, §14.2, §11.13; plan M4 Task
// 30, resolution e). It shows what the activation checks and re-scores, from the version's `GET …/{id}/
// activation-preview`: the eval gate of the candidate, the latest golden run, the schema verification and the
// models it does not cover, and the ads re-scored with their cost. A failed gate needs the override tick first
// ("Activate anyway — I accept the failed evaluation").
//
// **The preview is current when the dialog decides** (Ruling T30-d: an acknowledgement is given against the
// current figures). The preview is keyed by the version and by the form's base settings version, so a save reads
// it again, and the dialog reads it again every time it opens. Until that read is in, the dialog shows
// "Checking…" over a skeleton, never the figures of an older read: the override tick is not decided and
// Activate is busy. A failed read is a `ProblemBox` with Retry, and Activate is disabled (not busy) beside it.
//
// [Activate] sends the keyed `POST …/{id}/activate {expected_rescore_ack: true, eval_override_ack?: true}`
// (`eval_override_ack` only when ticked). On 201: the notice "Activated", then the form's `rebase()` (the
// activation saved a settings version), and the version lists, the header status and the Jobs list are read
// again, since the re-score starts. These run in the hook's own `onSuccess`, so also when the dialog is gone. A
// refusal stays in the dialog: 409 `conflict_state` with the detail `schema_dry_run_required` is "This
// version's schema is not verified for every model in use", naming the preview's unverified models as codes
// (the server's title only when the preview names none, D39); 409 `eval_gate_failed` (`isEvalGate`) offers the
// tick; any other problem is a `ProblemBox`. The notice, the eval status (`GOLDEN_RUNS_KEY`), the version
// lists, the header status and the Jobs list are read again after an activation.
import { useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { Fragment, useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { apiSend, type ProblemError } from "../api/client";
import { useKeyedMutation } from "../api/idempotency";
import type { components } from "../api/schema";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useToast } from "../components/Toaster";
import { useServerMessage, useServerText } from "../i18n/index";
import { JOBS_KEY } from "../jobs/api";
import { STATUS_KEY } from "../system/api";
import { useFormat } from "../system/format";
import styles from "./ActivationDialog.module.css";
import routing from "./CriticRouting.module.css";
import { GOLDEN_RUNS_KEY, useEvalResult } from "./EvalStatus";
import { isEvalGate, useSettingsForm } from "./useSettingsForm";

export type ActivationPreviewOut = components["schemas"]["ActivationPreviewOut"];
type ActivatedOut = components["schemas"]["ActivatedOut"];
type GoldenRunOut = components["schemas"]["GoldenRunOut"];
/** The two kinds of shipped version (`settings.activate.ShippedKind`). */
export type ShippedKind = "rubric" | "prompt_schema";

/** The activation's body: the re-score acknowledged, and the override only when ticked. */
interface ActivateBody {
  expected_rescore_ack: true;
  eval_override_ack?: true;
}

const PATHS = {
  rubric: "/api/settings/rubric-versions",
  prompt_schema: "/api/settings/prompt-schema-versions",
} as const satisfies Record<ShippedKind, string>;
/** The 409 `conflict_state` detail of a model policy without a passed live schema dry-run (`settings.save`). */
const SCHEMA_DRY_RUN_REQUIRED = "schema_dry_run_required";

/** The shipped versions' reads: each kind's list, and below it each version's preview under each base settings
 * version. */
export const SHIPPED_KEY = ["shipped-versions"] as const;
export const shippedListKey = (kind: ShippedKind) => [...SHIPPED_KEY, kind] as const;
const previewKey = (kind: ShippedKind, id: number, base: number) => [...SHIPPED_KEY, kind, id, "preview", base] as const;

/** The list of `kind`'s versions, by id. */
export function shippedPath(kind: ShippedKind): string {
  return PATHS[kind];
}

/** What activating `kind`'s version `id` would check and re-score under the form's base settings version, read
 * once for each base (the notice's figures); the dialog reads it again on each open. */
export function useActivationPreview(kind: ShippedKind, id: number): UseQueryResult<ActivationPreviewOut, ProblemError> {
  const base = useSettingsForm().base.version.id;
  return useQuery({
    queryKey: previewKey(kind, id, base),
    queryFn: ({ signal }) => apiSend<ActivationPreviewOut>(`${PATHS[kind]}/${String(id)}/activation-preview`, { signal }),
    staleTime: Infinity,
  });
}

/** The preview as the dialog decides on it (module docstring): read again since it opened, else `reading`. */
function useFreshPreview(kind: ShippedKind, id: number): {
  preview: UseQueryResult<ActivationPreviewOut, ProblemError>;
  reading: boolean;
  fresh: ActivationPreviewOut | undefined;
} {
  const preview = useActivationPreview(kind, id);
  const { refetch } = preview;
  const [opened, setOpened] = useState(false);
  useEffect(() => {
    let open = true;
    // a new read, whatever an earlier one holds or still waits for (the notice's)
    void refetch().then(() => {
      if (open) {
        setOpened(true);
      }
    });
    return () => {
      open = false;
    };
  }, [refetch]);
  const reading = !opened || preview.isFetching;
  return { preview, reading, fresh: !reading && preview.isSuccess ? preview.data : undefined };
}

/** The activation of `kind`'s version `id` (module docstring). */
function useActivate(kind: ShippedKind, id: number) {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const form = useSettingsForm();
  return useKeyedMutation(
    (body: ActivateBody, key: string) =>
      apiSend<ActivatedOut>(`${PATHS[kind]}/${String(id)}/activate`, { method: "POST", body: JSON.stringify(body), idempotencyKey: key }),
    {
      onSuccess: () => {
        toast({ message: t("activation.activated") });
        void form.rebase();
        void queryClient.invalidateQueries({ queryKey: SHIPPED_KEY });
        void queryClient.invalidateQueries({ queryKey: GOLDEN_RUNS_KEY });
        void queryClient.invalidateQueries({ queryKey: STATUS_KEY });
        void queryClient.invalidateQueries({ queryKey: JOBS_KEY });
      },
    },
  );
}

/** A golden run as the dialog shows it: `#7 · passed, ρ = 0.71 · 2 Nov, 09:40`. */
function useRunText(): (run: GoldenRunOut | null) => string {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const result = useEvalResult();
  return (run) => {
    if (run === null) {
      return t("activation.latest_run_none");
    }
    const state = run.passed === null ? serverText("eval_run_status", "completed") : serverText("eval_gate", run.passed ? "passed" : "failed");
    const text = result(state, run.spearman_rho);
    const id = String(run.eval_run_id);
    return run.finished_at === null
      ? t("activation.latest_run_undated", { id, result: text })
      : t("activation.latest_run_value", { id, result: text, date: format.dayMonthTime(run.finished_at) });
  };
}

function Preview({ preview }: { preview: ActivationPreviewOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const runText = useRunText();
  let schema: string;
  if (preview.schema_verified === null) {
    schema = t("activation.schema_unchecked");
  } else if (preview.schema_verified) {
    schema = t("activation.schema_verified");
  } else {
    schema = t("activation.schema_unverified", { models: preview.unverified_models.join(", ") });
  }
  const rows = [
    { key: "gate", term: t("activation.gate"), value: serverText("eval_gate", preview.gate_state) },
    { key: "run", term: t("activation.latest_run"), value: runText(preview.latest_golden_run) },
    { key: "schema", term: t("activation.schema"), value: schema },
    {
      key: "rescore",
      term: t("activation.rescore"),
      value: t("activation.rescore_value", {
        count: preview.targets,
        value: format.number(preview.targets),
        online: format.usd(preview.est_usd_online),
        batch: format.usd(preview.est_usd_batch),
        bulk: format.usd(preview.bulk_budget_usd),
      }),
    },
  ];
  return (
    <dl className={styles.preview}>
      {rows.map((row) => (
        <div key={row.key} className={routing.figure} data-tone={(row.key === "gate" && preview.gate_state === "failed") || (row.key === "schema" && preview.schema_verified === false) ? "warn" : undefined}>
          <dt>{row.term}</dt>
          <dd>{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Models as codes, in a list a translated sentence holds (`activation.schema_required_models`). */
function Codes({ models }: { models: readonly string[] }) {
  return (
    <>
      {models.map((model, index) => (
        <Fragment key={model}>
          {index > 0 && ", "}
          <code>{model}</code>
        </Fragment>
      ))}
    </>
  );
}

function schemaRequired(error: ProblemError): boolean {
  return error.status === 409 && error.code === "conflict_state" && error.detail === SCHEMA_DRY_RUN_REQUIRED;
}

export interface ActivationDialogProps {
  kind: ShippedKind;
  versionId: number;
  /** The version as the title names it: a rubric's version (`v2`), a prompt/schema version's hash prefix. */
  name: string;
  onClose: () => void;
}

export function ActivationDialog({ kind, versionId, name, onClose }: ActivationDialogProps) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const serverMessage = useServerMessage();
  const { preview, reading, fresh } = useFreshPreview(kind, versionId);
  const activation = useActivate(kind, versionId);
  const [override, setOverride] = useState(false);
  // a refusal of the gate is kept once seen: the tick stays while a retry runs and after it fails otherwise
  const [gateRefused, setGateRefused] = useState(false);
  const error = activation.error;
  const failed = !reading && preview.isError;
  const needsOverride = fresh?.gate_state === "failed" || gateRefused;
  const submit = () => {
    if (fresh === undefined || activation.isPending || (needsOverride && !override)) {
      return;
    }
    const body: ActivateBody = { expected_rescore_ack: true, ...(needsOverride && override ? { eval_override_ack: true as const } : {}) };
    activation.mutate(body, {
      onSuccess: onClose,
      onError: (refusal) => {
        if (isEvalGate(refusal)) {
          setGateRefused(true);
          setOverride(false);
        }
      },
    });
  };
  const title = kind === "rubric" ? t("activation.title_rubric", { version: name }) : t("activation.title_prompt", { hash: name });
  return (
    <ConfirmDialog open title={title} confirmLabel={t("activation.confirm")} onCancel={onClose} onConfirm={submit}
      busy={activation.isPending || reading} confirmDisabled={failed || (needsOverride && !override)}>
      <div className={styles.content}>
        {fresh !== undefined && <Preview preview={fresh} />}
        {reading && (
          <>
            <p className={styles.checking}>{t("settings_form.checking")}</p>
            <Skeleton variant="line" count={3} />
          </>
        )}
        {failed && <ProblemBox error={preview.error} onRetry={() => { void preview.refetch(); }} />}
        {needsOverride && (
          <div className={styles.override}>
            {gateRefused && <p className={styles.overrideTitle}>{serverText("problem", "eval_gate_failed")}</p>}
            <label className={styles.tick}>
              <input type="checkbox" checked={override} onChange={(event) => { setOverride(event.target.checked); }} />
              <span>{t("activation.override")}</span>
            </label>
          </div>
        )}
        {error !== null && schemaRequired(error) && (
          <div role="alert" className={styles.problem}>
            <p className={styles.problemTitle}>{t("activation.schema_required")}</p>
            {fresh !== undefined && fresh.unverified_models.length > 0 ? (
              <p>
                <Trans i18nKey="activation.schema_required_models" components={{ models: <Codes models={fresh.unverified_models} /> }} />
              </p>
            ) : (
              // the server's title names the node and model pairs, in English: shown where §15.9 lets it
              serverMessage("problem", error.code, error.message) !== null && <p>{error.message}</p>
            )}
          </div>
        )}
        {error !== null && !isEvalGate(error) && !schemaRequired(error) && <ProblemBox error={error} />}
      </div>
    </ConfirmDialog>
  );
}
