// The re-score confirmation (spec §12.6 step 2, §15.7 L5294–5297; plan M4 Task 27, resolution h).
//
// It reads `GET …/rescore-estimate` when it opens and says what confirming does in one sentence per language,
// interpolated, never put together from fragments: counts by `Intl.NumberFormat`, amounts by `format.usd`, the
// batch share as a percentage. While the estimate loads a skeleton line stands in for it and Confirm is
// disabled. [Confirm & re-score] sends the keyed confirm with `expected_rescore_ack: true`; it is busy while the
// request runs, so a second press sends nothing. A 409 shows its problem here; a 422 closes the modal and hands
// its errors to the form, which shows them at their fields.
import { useTranslation } from "react-i18next";
import type { FieldError } from "../../api/client";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { ProblemBox } from "../../components/ProblemBox";
import { Skeleton } from "../../components/Skeleton";
import { intlLocale } from "../../i18n/index";
import { useFormat } from "../../system/format";
import { type RescoreEstimateOut, useConfirmDraft, useRescoreEstimate } from "./api";

export interface ConfirmModalProps {
  draftId: number;
  onCancel: () => void;
  /** A 422: the confirm's errors, for the form to show at their fields. */
  onInvalid: (errors: FieldError[]) => void;
}

function useSentence(): (estimate: RescoreEstimateOut) => string {
  const { t, i18n } = useTranslation();
  const format = useFormat();
  const percent = new Intl.NumberFormat(intlLocale(i18n.language), { style: "percent" });
  return (e) =>
    t("cv.confirm.sentence", {
      count: e.targets,
      version: e.next_version_no,
      targets: format.number(e.targets),
      budget: format.usd(e.bulk_budget_usd),
      share: percent.format(Number(e.bulk_share)),
      batch: format.number(e.batch_targets),
      online: format.number(e.online_targets),
      per_day: format.number(e.online_per_day),
      spend: format.usd(e.month_spend),
      cap: format.usd(e.cap),
    });
}

export function ConfirmModal({ draftId, onCancel, onInvalid }: ConfirmModalProps) {
  const { t } = useTranslation();
  const sentence = useSentence();
  const estimate = useRescoreEstimate(draftId);
  const confirm = useConfirmDraft(draftId, estimate.data?.next_version_no);
  const submit = () => {
    if (estimate.data === undefined || confirm.isPending) {
      return;
    }
    confirm.mutate(undefined, {
      onError: (error) => {
        if (error.status === 422) {
          onInvalid(error.errors);
        }
      },
    });
  };
  return (
    <ConfirmDialog open title={t("cv.confirm.title")} confirmLabel={t("cv.confirm.confirm")} onCancel={onCancel}
      onConfirm={submit} busy={confirm.isPending || estimate.data === undefined}>
      {estimate.data !== undefined ? <p>{sentence(estimate.data)}</p> : estimate.isError ? null : <Skeleton variant="line" />}
      {estimate.error !== null && <ProblemBox error={estimate.error} onRetry={() => { void estimate.refetch(); }} />}
      {confirm.error !== null && confirm.error.status !== 422 && <ProblemBox error={confirm.error} />}
    </ConfirmDialog>
  );
}
