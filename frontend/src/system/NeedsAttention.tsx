// System → Needs attention (spec §14.2 L5040-5044, §15.7; plan M4 Task 31, resolution d, U1, U3): one block per
// section of `AttentionOut`, in `observability/attention.py`'s order. Each block shows its heading with the
// section's full count, its items (the server sends at most 50), "and n more" when the count exceeds them, and
// "Nothing here" when it is empty. There is no failed-purges block (U1: M5's).
//
// Each item's action disables its button while it runs; once answered, Needs attention and the header's status
// are read again (`systemApi`); a refusal shows as a `ProblemBox` at the item.
// - **Rejected raw items**: [Replay] (`{raw_response_id}`) on a replayable response; another says why, as text.
// - **Identity conflicts**: [Keep apart] and [Merge], each behind a `ConfirmDialog` naming the jobs.
// - **Refused and failed scores**, and the members of an unscorable group whose target is refused or failed
//   (`scoring.targets.RETRYABLE_STATES`, the states the retry route takes): [Retry].
// - **Quarantined batches** (a `submit_ambiguous` one; a `released` one is listed while it is reconciled) and
//   **batches that cannot be retrieved** (only when `releasable`, U3): [Release & re-score] opens a `ConfirmDialog`
//   stating that item's `reserved_usd`, which stays counted as ambiguous spend; only its Confirm sends the keyed
//   `{confirm: true}`. A retrieve-failing batch that is not releasable yet says from when it can be: its
//   `expires_at` plus the hour the route waits (`releasable`: "more than 1 h past its expiry").
// - **Mass disappearances**: [Confirm closure] behind a `ConfirmDialog`, since it closes postings.
// - **Paused scoring**: [Acknowledge] sends the pausing alert's acknowledgement.
// - **Failed scope apply**: [Retry apply] (`useRetryApply`).
// - **Broken sources** are information only; a `degraded` one shows as a warning.
//
// A job an item names is a `JobRef`, which opens the job in the Jobs page's drawer (`/?job=<id>`).
import { useNavigate } from "@tanstack/react-router";
import type { TFunction } from "i18next";
import { type ReactNode, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { cx } from "../components/cx";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useToast } from "../components/Toaster";
import { intlLocale, useServerText } from "../i18n/index";
import { JobRef } from "../jobs/JobRef";
import { closedSet } from "../settings/fields";
import fields from "../settings/fields.module.css";
import { useRetryApply } from "./api";
import { type Format, useFormat } from "./format";
import styles from "./NeedsAttention.module.css";
import parts from "./SystemSection.module.css";
import {
  type AttentionOut,
  type Decision,
  useAckAlert,
  useAfterRetryApply,
  useAttention,
  useConfirmClosure,
  useReleaseBatch,
  useReplay,
  useResolveConflict,
  useRetryTarget,
} from "./systemApi";

type Section = keyof AttentionOut;
type Item<S extends Section> = AttentionOut[S] extends { items: (infer I)[] } ? I : never;

/** The sections in the module's order: `AttentionOut`'s every key, each once. */
export const SECTIONS = closedSet<Section>({
  raw_items: true,
  identity_conflicts: true,
  failed_targets: true,
  quarantined_batches: true,
  retrieve_failing: true,
  mass_disappearances: true,
  unscorable_groups: true,
  broken_sources: true,
  paused_lane: true,
  scope_apply_failed: true,
});

/** The target states the retry route takes back (`scoring.targets.RETRYABLE_STATES`). */
const RETRYABLE: ReadonlySet<string> = new Set([
  "refused",
  "failed_max_tokens",
  "failed_invalid_output",
  "failed_request",
  "failed_transient",
  "failed_attempts_exhausted",
]);

/** How long after its expiry a lost batch can be released (`releasable`: "more than 1 h past its expiry";
 * `scoring.batches.RETRIEVE_FAILING_AFTER`). */
const RELEASE_AFTER_EXPIRY_MS = 3_600_000;

function useOpenJob(): (jobId: number) => void {
  const navigate = useNavigate();
  return (jobId) => { void navigate({ to: "/", search: { job: jobId } }); };
}

function jobName(t: TFunction, jobId: number): string {
  return t("drawer.job_ref", { id: String(jobId) });
}

/** An item: what it is, its action, and below them its refusal. */
function ItemRow({ children, actions, error }: { children: ReactNode; actions?: ReactNode; error?: unknown }) {
  return (
    <li className={styles.item}>
      <div className={styles.what}>{children}</div>
      {actions !== undefined && <div className={styles.actions}>{actions}</div>}
      {error != null && <div className={styles.problem}><ProblemBox error={error} /></div>}
    </li>
  );
}

function Meta({ children }: { children: ReactNode }) {
  return <p className={styles.meta}>{children}</p>;
}

function Block({ name, count, shown, children }: { name: Section; count: number; shown: number; children?: ReactNode }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const id = useId();
  const more = count - shown;
  return (
    <section aria-labelledby={id} className={styles.block}>
      <h4 id={id} className={styles.blockTitle}>
        <span>{serverText("attention_section", name)}</span>{" "}
        <span className={cx(styles.count, count > 0 && styles.countSome)}>{format.number(count)}</span>
      </h4>
      {count === 0 ? (
        <p className={styles.nothing}>{t("attention.nothing")}</p>
      ) : (
        <ul role="list" className={styles.items}>{children}</ul>
      )}
      {more > 0 && <p className={styles.more}>{t("attention.more", { count: more, value: format.number(more) })}</p>}
    </section>
  );
}

function RawItem({ item }: { item: Item<"raw_items"> }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const toast = useToast();
  const replay = useReplay();
  return (
    <ItemRow error={replay.error}
      actions={item.replayable ? (
        <button type="button" className={fields.button} disabled={replay.isPending}
          onClick={() => {
            replay.mutate(item.raw_response_id, {
              onSuccess: (out) => { toast({ message: t("attention.replay_queued", { count: out.items, value: format.number(out.items) }) }); },
            });
          }}>
          {t("attention.replay")}
        </button>
      ) : undefined}>
      <p className={styles.title}>{item.instance_label}</p>
      <Meta>
        {t("attention.raw_meta", { source: serverText("source", item.source_id), time: format.dayMonthTime(item.fetched_at) })}
        {" · "}
        {t("attention.raw_rejected", { count: item.rejected, value: format.number(item.rejected) })}
      </Meta>
      <p className={styles.codes}>
        <span className={styles.meta}>{t("attention.reasons")}</span>
        {item.reasons.map((reason) => <code key={reason} className={styles.code}>{reason}</code>)}
      </p>
      {!item.replayable && <p className={styles.why}>{t("attention.not_replayable")}</p>}
    </ItemRow>
  );
}

function ConflictItem({ item }: { item: Item<"identity_conflicts"> }) {
  const { t, i18n } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const toast = useToast();
  const openJob = useOpenJob();
  const resolve = useResolveConflict();
  const [asking, setAsking] = useState<Decision | null>(null);
  const list = new Intl.ListFormat(intlLocale(i18n.language), { type: "conjunction" });
  const [posting, ...others] = item.job_ids;
  const decide = (decision: Decision) => {
    setAsking(null);
    resolve.mutate({ conflictId: item.conflict_id, decision }, { onSuccess: () => { toast({ message: t("attention.resolved") }); } });
  };
  const body =
    asking === "merge"
      ? t("attention.merge_body", { count: others.length, others: list.format(others.map((id) => jobName(t, id))), posting: jobName(t, posting) })
      : t("attention.separate_body", { jobs: list.format(item.job_ids.map((id) => jobName(t, id))) });
  return (
    <ItemRow error={resolve.error}
      actions={
        <>
          <button type="button" className={fields.button} disabled={resolve.isPending} onClick={() => { setAsking("separate"); }}>
            {t("attention.separate")}
          </button>
          <button type="button" className={fields.button} disabled={resolve.isPending} onClick={() => { setAsking("merge"); }}>
            {t("attention.merge")}
          </button>
        </>
      }>
      <p className={styles.title}>{t("attention.conflict_title", { id: String(item.conflict_id) })}</p>
      <p className={styles.jobs}>
        <span className={styles.meta}>{t("attention.jobs")}</span>
        {item.job_ids.map((id) => <JobRef key={id} jobId={id} onNavigate={openJob} />)}
      </p>
      <Meta>
        {i18n.exists(`conflict_reason.${item.reason}`) ? (
          serverText("conflict_reason", item.reason)
        ) : (
          <>{t("attention.conflict_keys")} <code className={styles.code}>{item.reason}</code></>
        )}
        {" · "}
        {t("attention.opened", { time: format.dayMonthTime(item.opened_at) })}
      </Meta>
      <ConfirmDialog open={asking !== null}
        title={asking === "merge" ? t("attention.merge_title") : t("attention.separate_title")}
        confirmLabel={asking === "merge" ? t("attention.merge_confirm") : t("attention.separate_confirm")}
        onCancel={() => { setAsking(null); }}
        onConfirm={() => { if (asking !== null) { decide(asking); } }}>
        <p>{body}</p>
      </ConfirmDialog>
    </ItemRow>
  );
}

/** [Retry] of a target, with its refusal shown by the caller's item (`onError`). */
function RetryButton({ targetId, retry }: { targetId: number; retry: ReturnType<typeof useRetryTarget> }) {
  const { t } = useTranslation();
  const toast = useToast();
  return (
    <button type="button" className={fields.button} disabled={retry.isPending}
      onClick={() => { retry.mutate(targetId, { onSuccess: () => { toast({ message: t("attention.retried") }); } }); }}>
      {t("attention.retry")}
    </button>
  );
}

function FailedTargetItem({ item }: { item: Item<"failed_targets"> }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const openJob = useOpenJob();
  const retry = useRetryTarget();
  return (
    <ItemRow error={retry.error} actions={<RetryButton targetId={item.target_id} retry={retry} />}>
      <p className={styles.title}><JobRef jobId={item.job_id} title={item.title} onNavigate={openJob} /></p>
      <Meta>
        {serverText("target_state", item.state)}
        {item.refusal_category !== null && <> · {t("attention.refusal_category", { category: item.refusal_category })}</>}
      </Meta>
      {item.state_reason !== null && <p className={styles.codes}><code className={styles.code}>{item.state_reason}</code></p>}
    </ItemRow>
  );
}

/** [Release & re-score], asked first with the batch's reserved amount; the keyed release only on Confirm. */
function ReleaseButton({ submissionId, reservedUsd, release }: {
  submissionId: number;
  reservedUsd: string;
  release: ReturnType<typeof useReleaseBatch>;
}) {
  const { t } = useTranslation();
  const format = useFormat();
  const toast = useToast();
  const [asking, setAsking] = useState(false);
  return (
    <>
      <button type="button" className={fields.button} disabled={release.isPending} onClick={() => { setAsking(true); }}>
        {t("attention.release")}
      </button>
      <ConfirmDialog open={asking} title={t("attention.release_title")} confirmLabel={t("attention.release_confirm")}
        onCancel={() => { setAsking(false); }}
        onConfirm={() => {
          setAsking(false);
          release.mutate(submissionId, { onSuccess: () => { toast({ message: t("attention.released") }); } });
        }}>
        <p>{t("attention.release_body", { usd: format.usd(reservedUsd) })}</p>
      </ConfirmDialog>
    </>
  );
}

function QuarantinedItem({ item }: { item: Item<"quarantined_batches"> }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const release = useReleaseBatch();
  return (
    <ItemRow error={release.error}
      actions={item.state === "submit_ambiguous" ? (
        <ReleaseButton submissionId={item.submission_id} reservedUsd={item.reserved_usd} release={release} />
      ) : undefined}>
      <p className={styles.title}>
        {t("attention.batch_title", { id: String(item.submission_id) })}
        <span className={styles.state}>{serverText("batch_state", item.state)}</span>
      </p>
      <Meta>
        {t("attention.batch_meta", {
          count: item.request_count,
          value: format.number(item.request_count),
          usd: format.usd(item.reserved_usd),
          time: format.dayMonthTime(item.submit_started_at),
        })}
      </Meta>
      <Meta>
        {item.last_reconcile_at === null
          ? t("attention.not_reconciled")
          : t("attention.reconciled", { time: format.dayMonthTime(item.last_reconcile_at) })}
        {item.unread_candidates > 0 && (
          <> · {t("attention.unread", { count: item.unread_candidates, value: format.number(item.unread_candidates) })}</>
        )}
      </Meta>
    </ItemRow>
  );
}

function releasableFrom(expiresAt: string | null, format: Format): string | null {
  return expiresAt === null ? null : format.dayMonthTime(new Date(Date.parse(expiresAt) + RELEASE_AFTER_EXPIRY_MS).toISOString());
}

function RetrieveFailingItem({ item }: { item: Item<"retrieve_failing"> }) {
  const { t } = useTranslation();
  const format = useFormat();
  const release = useReleaseBatch();
  const from = item.releasable ? null : releasableFrom(item.expires_at, format);
  return (
    <ItemRow error={release.error}
      actions={item.releasable ? (
        <ReleaseButton submissionId={item.submission_id} reservedUsd={item.reserved_usd} release={release} />
      ) : undefined}>
      <p className={styles.title}>{t("attention.batch_title", { id: String(item.submission_id) })}</p>
      <p className={styles.codes}>
        <span className={styles.meta}>{t("attention.provider_batch")}</span>
        <code className={styles.code}>{item.provider_batch_id}</code>
      </p>
      <Meta>
        {t("attention.reserved", { usd: format.usd(item.reserved_usd) })}
        {item.expires_at !== null && <> · {t("attention.expires", { time: format.dayMonthTime(item.expires_at) })}</>}
      </Meta>
      {from !== null && <p className={styles.why}>{t("attention.release_from", { time: from })}</p>}
    </ItemRow>
  );
}

function MassItem({ item }: { item: Item<"mass_disappearances"> }) {
  const { t } = useTranslation();
  const format = useFormat();
  const toast = useToast();
  const closure = useConfirmClosure();
  const [asking, setAsking] = useState(false);
  const time = format.dayMonthTime(item.taken_at);
  return (
    <ItemRow error={closure.error}
      actions={
        <button type="button" className={fields.button} disabled={closure.isPending} onClick={() => { setAsking(true); }}>
          {t("attention.confirm_closure")}
        </button>
      }>
      <p className={styles.title}>{item.instance_label}</p>
      <Meta>
        {item.absent_count === null || item.baseline_active_count === null
          ? t("attention.mass_meta_unknown", { time })
          : t("attention.mass_meta", { absent: format.number(item.absent_count), baseline: format.number(item.baseline_active_count), time })}
      </Meta>
      <ConfirmDialog open={asking} title={t("attention.closure_title")} confirmLabel={t("attention.confirm_closure")}
        onCancel={() => { setAsking(false); }}
        onConfirm={() => {
          setAsking(false);
          closure.mutate(item.snapshot_id, {
            onSuccess: (out) => { toast({ message: t("attention.closed", { count: out.closed, value: format.number(out.closed) }) }); },
          });
        }}>
        <p>{t("attention.closure_body", { label: item.instance_label })}</p>
      </ConfirmDialog>
    </ItemRow>
  );
}

function MemberRow({ member }: { member: Item<"unscorable_groups">["members"][number] }) {
  const serverText = useServerText();
  const openJob = useOpenJob();
  const retry = useRetryTarget();
  const retryable = member.target_id !== null && member.target_state !== null && RETRYABLE.has(member.target_state);
  return (
    <ItemRow error={retry.error}
      actions={retryable && member.target_id !== null ? <RetryButton targetId={member.target_id} retry={retry} /> : undefined}>
      <p className={styles.title}><JobRef jobId={member.job_id} title={member.title} onNavigate={openJob} /></p>
      {member.target_state !== null && <Meta>{serverText("target_state", member.target_state)}</Meta>}
    </ItemRow>
  );
}

function GroupItem({ item }: { item: Item<"unscorable_groups"> }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  return (
    <li className={styles.group}>
      <p className={styles.title}>
        {t("attention.group_title", { id: String(item.group_id) })}
        <span className={styles.state}>{serverText("group_unscorable", item.unscorable_reason)}</span>
      </p>
      <ul role="list" className={styles.members}>
        {item.members.map((member) => <MemberRow key={member.job_id} member={member} />)}
      </ul>
    </li>
  );
}

function BrokenItem({ item }: { item: Item<"broken_sources"> }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const warn = item.severity === "warn";
  return (
    <li className={cx(styles.item, styles.info, warn ? styles.warn : styles.critical)}>
      <div className={styles.what}>
        <p className={styles.title}>
          {item.label}
          <span className={cx(styles.badge, warn ? styles.badgeWarn : styles.badgeCritical)}>{serverText("alert_severity", item.severity)}</span>
        </p>
        <Meta>
          {serverText("source", item.source_id)} · {serverText("instance_health", item.health)}
          {item.breaker_opened_at !== null && <> · {t("attention.breaker_since", { time: format.dayMonthTime(item.breaker_opened_at) })}</>}
          {item.half_open_after !== null && <> · {t("attention.next_try", { time: format.dayMonthTime(item.half_open_after) })}</>}
        </Meta>
        {item.last_error_code !== null && (
          <p className={styles.codes}>
            <span className={styles.meta}>{t("attention.last_error")}</span>
            <code className={styles.code}>{item.last_error_code}</code>
          </p>
        )}
      </div>
    </li>
  );
}

function PausedItem({ lane }: { lane: NonNullable<AttentionOut["paused_lane"]> }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const toast = useToast();
  const ack = useAckAlert();
  const alertId = lane.alert_id;
  return (
    <ItemRow error={ack.error}
      actions={alertId === null ? undefined : (
        <button type="button" className={fields.button} disabled={ack.isPending}
          onClick={() => { ack.mutate(alertId, { onSuccess: () => { toast({ message: t("attention.acked") }); } }); }}>
          {t("attention.ack")}
        </button>
      )}>
      <p className={styles.title}>{t("attention.paused", { reason: serverText("pause", lane.reason) })}</p>
    </ItemRow>
  );
}

function ScopeItem({ failed }: { failed: NonNullable<AttentionOut["scope_apply_failed"]> }) {
  const { t } = useTranslation();
  const toast = useToast();
  const retry = useRetryApply();
  const refresh = useAfterRetryApply();
  return (
    <ItemRow error={retry.error}
      actions={
        <button type="button" className={fields.button} disabled={retry.isPending}
          onClick={() => {
            retry.mutate(undefined, {
              onSuccess: () => {
                refresh();
                toast({ message: t("banner.retry_apply_started") });
              },
            });
          }}>
          {t("banner.retry_apply")}
        </button>
      }>
      <p className={styles.title}>{t("attention.scope_failed", { version: String(failed.version_id) })}</p>
    </ItemRow>
  );
}

function listed<S extends Exclude<Section, "paused_lane" | "scope_apply_failed">>(
  data: AttentionOut,
  name: S,
  render: (item: Item<S>, index: number) => ReactNode,
): ReactNode {
  const section = data[name] as { count: number; items: Item<S>[] };
  return (
    <Block key={name} name={name} count={section.count} shown={section.items.length}>
      {section.items.map(render)}
    </Block>
  );
}

function blockOf(data: AttentionOut, name: Section): ReactNode {
  switch (name) {
    case "raw_items":
      return listed(data, name, (item) => <RawItem key={item.raw_response_id} item={item} />);
    case "identity_conflicts":
      return listed(data, name, (item) => <ConflictItem key={item.conflict_id} item={item} />);
    case "failed_targets":
      return listed(data, name, (item) => <FailedTargetItem key={item.target_id} item={item} />);
    case "quarantined_batches":
      return listed(data, name, (item) => <QuarantinedItem key={item.submission_id} item={item} />);
    case "retrieve_failing":
      return listed(data, name, (item) => <RetrieveFailingItem key={item.submission_id} item={item} />);
    case "mass_disappearances":
      return listed(data, name, (item) => <MassItem key={item.snapshot_id} item={item} />);
    case "unscorable_groups":
      return listed(data, name, (item) => <GroupItem key={item.group_id} item={item} />);
    case "broken_sources":
      return listed(data, name, (item) => <BrokenItem key={item.instance_id} item={item} />);
    case "paused_lane": {
      const lane = data.paused_lane;
      return (
        <Block key={name} name={name} count={lane === null ? 0 : 1} shown={lane === null ? 0 : 1}>
          {lane !== null && <PausedItem lane={lane} />}
        </Block>
      );
    }
    case "scope_apply_failed": {
      const failed = data.scope_apply_failed;
      return (
        <Block key={name} name={name} count={failed === null ? 0 : 1} shown={failed === null ? 0 : 1}>
          {failed !== null && <ScopeItem failed={failed} />}
        </Block>
      );
    }
  }
}

export function NeedsAttention() {
  const { t } = useTranslation();
  const attention = useAttention();
  let body: ReactNode;
  if (attention.isPending) {
    body = <Skeleton variant="line" count={4} />;
  } else if (attention.isError) {
    body = <ProblemBox error={attention.error} onRetry={() => { void attention.refetch(); }} />;
  } else {
    body = <div className={styles.blocks}>{SECTIONS.map((name) => blockOf(attention.data, name))}</div>;
  }
  return (
    <section id="needs-attention" aria-labelledby="needs-attention-heading" className={parts.part}>
      <h3 id="needs-attention-heading" className={parts.title}>{t("attention.title")}</h3>
      {body}
    </section>
  );
}
