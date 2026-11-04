// A duplicate group's members (spec §15.5, §15.4's Duplicates tab, §8.7; plan M4 Task 25, resolution c): the
// `GroupOut` of the representative's detail (`GET /api/jobs/{job_id}`'s `group`), on the group card when it is
// expanded and in the drawer's Duplicates tab.
//
// - The members are listed the representative first (marked, with its election reason, a code shown verbatim:
//   no closed set to translate it from), then by job id as the api lists them. Each shows its sources, title,
//   company and places, its completeness, availability and own state (its active-tuple target's), and "Scored
//   individually" once the user asked for that; the drawer marks the opened job "This job". With `onOpen`, the
//   title of every other member is a button that shows that member's detail. A member, and the group of its
//   actions, is named by its title and its sources, company and places, since duplicates often share a title.
// - Split is on every member: it needs no confirmation, since a group is never formed again behind the user's
//   back (§8.7). Score individually is on a member other than the representative not scored individually yet.
//   Confirm as one job, once below the list while the group is active and has a representative, asks first.
// - Split and Score individually answer the group after the write: it goes into the cached detail of each
//   member that stays (the representative's is the group card's), and a split member's says it is in no group,
//   so the list changes at once; every job query is read again (`JOBS_KEY`), and after Score individually and
//   Confirm the header's status too (a member becomes pending, or the merged members' targets are cancelled).
//   Confirm answers 202: "Merged into one job". A dissolved group (a split that left one member, or a
//   confirmation) says "The group was dissolved" or the merge, and its cached members carry the dissolved
//   group, which the group card and the tab show as gone. `onGone` names, before the caches change, the members
//   whose group a write ended (the group card then knows the list is being read again already).
// - None of these routes takes an `Idempotency-Key` (§14.1). While a write runs every action of the group is
//   disabled (`aria-busy` on the list), so writes never overlap. A retryable 409 (a concurrent change, the
//   route's three tries spent) offers Try again, which sends the same request; any other failure is the problem
//   box. When a write takes the focused control away (a split member leaves the list), focus goes to the list.
import { useMutation, type UseMutationResult, useQueryClient } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { useId, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { apiFetch, type ProblemError, toProblem } from "../api/client";
import type { components } from "../api/schema";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ProblemBox } from "../components/ProblemBox";
import { useToast } from "../components/Toaster";
import { intlLocale, useServerText } from "../i18n/index";
import { STATUS_KEY } from "../system/api";
import { JOBS_KEY } from "./api";
import { detailKey, type JobDetailOut } from "./detailApi";
import { availabilityText, type ServerText } from "./format";
import styles from "./GroupMembers.module.css";
import { focusDropped } from "./useListKeyboard";

export type GroupOut = components["schemas"]["GroupOut"];
type GroupMemberOut = components["schemas"]["GroupMemberOut"];
type ConfirmedOut = components["schemas"]["ConfirmedOut"];

/** A write on the group: a member's Split or Score individually, or Confirm as one job. */
type GroupWrite = { action: "split" | "score-individually"; jobId: number } | { action: "confirm" };

/** A write's answer: the group after it, or (Confirm) the job every other member was merged into. */
type Written = { group: GroupOut } | { mergedInto: number };

/** A job's sources by their product names, as one list ("JSearch and Greenhouse"). */
export function sourceNames(sourceIds: readonly string[], language: string, serverText: ServerText): string {
  return new Intl.ListFormat(intlLocale(language), { type: "conjunction" }).format(
    sourceIds.map((source) => serverText("source", source)),
  );
}

/** A member's own state: its active-tuple target's state name, "Not queued yet" without a target. */
export function memberStateText(state: string | null, t: TFunction, serverText: ServerText): string {
  return state === null ? t("chip.not_queued") : serverText("target_state", state);
}

/** The group once it is dissolved: what Confirm leaves behind (the last other member merged away). */
function dissolved(group: GroupOut): GroupOut {
  return { ...group, status: "dissolved", representative_job_id: null, unscorable_reason: null, members: [] };
}

function useGroupWrite(
  group: GroupOut,
  onGone?: (jobIds: readonly number[]) => void,
): UseMutationResult<Written, ProblemError, GroupWrite> {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  return useMutation<Written, ProblemError, GroupWrite>({
    mutationFn: async (write) => {
      const path = `/api/duplicate-groups/${String(group.id)}/${write.action}`;
      try {
        if (write.action === "confirm") {
          const out = await apiFetch<ConfirmedOut>(path, { method: "POST" });
          return { mergedInto: out.job_id };
        }
        return { group: await apiFetch<GroupOut>(path, { method: "POST", body: JSON.stringify({ job_id: write.jobId }) }) };
      } catch (error) {
        throw toProblem(error);
      }
    },
    retry: false,
    meta: { ownToast: true }, // its failure is shown under the members
    onSuccess: (written, write) => {
      const after = "group" in written ? written.group : dissolved(group);
      const split = write.action === "split" ? write.jobId : null; // in no group from now on
      const members = group.members.map((member) => member.job_id);
      const ended = after.status === "dissolved" ? members : members.filter((jobId) => jobId === split);
      if (ended.length > 0) {
        onGone?.(ended);
      }
      for (const jobId of members) {
        queryClient.setQueryData<JobDetailOut>(detailKey(jobId), (detail) =>
          detail === undefined ? detail : { ...detail, group: jobId === split ? null : after },
        );
      }
      if ("mergedInto" in written) {
        toast({ message: t("group.merged") });
      } else if (after.status === "dissolved") {
        toast({ message: t("group.dissolved") });
      }
      void queryClient.invalidateQueries({ queryKey: JOBS_KEY });
      if (write.action !== "split") {
        void queryClient.invalidateQueries({ queryKey: STATUS_KEY });
      }
    },
  });
}

/** Whether a failed write is a concurrent change the route gave up on after its tries (Try again). */
function retryable(error: ProblemError | null): boolean {
  return error !== null && error.status === 409 && error.extra.retryable === true;
}

interface MemberProps {
  member: GroupMemberOut;
  electionReason: string | null;
  thisJob: boolean;
  busy: boolean;
  onOpen?: (jobId: number) => void;
  onWrite: (write: GroupWrite) => void;
}

function Member({ member, electionReason, thisJob, busy, onOpen, onWrite }: MemberProps) {
  const { t, i18n } = useTranslation();
  const serverText = useServerText();
  const titleId = useId();
  const whereId = useId();
  const name = `${titleId} ${whereId}`; // the title, then its sources, company and places
  const { job_id: jobId } = member;
  const where = [sourceNames(member.source_ids, i18n.language, serverText), member.company, ...member.locations];
  const state = [
    serverText("completeness", member.completeness),
    availabilityText(member.availability, t),
    memberStateText(member.target_state, t, serverText),
  ];
  return (
    <li aria-labelledby={name} className={styles.member}>
      <p className={styles.head}>
        {onOpen !== undefined && !thisJob ? (
          <button id={titleId} type="button" className={styles.ref} onClick={() => { onOpen(jobId); }}>
            {member.title}
          </button>
        ) : (
          <span id={titleId} className={styles.title}>{member.title}</span>
        )}
        {member.is_representative && <> <span className={styles.marker}>{t("group.representative")}</span></>}
        {member.is_representative && electionReason !== null && (
          <>
            {" "}
            <span className={styles.muted}>
              {t("group.elected")} <code className={styles.code}>{electionReason}</code>
            </span>
          </>
        )}
        {thisJob && <> <span className={styles.marker}>{t("group.this_job")}</span></>}
        {member.score_individually && <> <span className={styles.marker}>{t("group.individual")}</span></>}
      </p>
      <p id={whereId} className={styles.facts}>{where.filter((part) => part !== "").join(" · ")}</p>
      <p className={styles.facts}>{state.join(" · ")}</p>
      <div role="group" aria-labelledby={name} className={styles.actions}>
        <button type="button" className={styles.button} disabled={busy}
          onClick={() => { onWrite({ action: "split", jobId }); }}>
          {t("group.split")}
        </button>
        {!member.is_representative && !member.score_individually && (
          <button type="button" className={styles.button} disabled={busy}
            onClick={() => { onWrite({ action: "score-individually", jobId }); }}>
            {t("group.score_individually")}
          </button>
        )}
      </div>
    </li>
  );
}

export interface GroupMembersProps {
  group: GroupOut;
  /** The job the drawer shows ("This job"). */
  thisJobId?: number;
  /** Shows a member's detail (a click on its title). */
  onOpen?: (jobId: number) => void;
  /** The members whose group a write of this list ended (a split member; every member of a dissolved or
   * confirmed group), named before the caches change. */
  onGone?: (jobIds: readonly number[]) => void;
}

export function GroupMembers({ group, thisJobId, onOpen, onGone }: GroupMembersProps) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const write = useGroupWrite(group, onGone);
  const [confirming, setConfirming] = useState(false);
  const busy = write.isPending;
  const members = [...group.members].sort((a, b) => Number(b.is_representative) - Number(a.is_representative));
  const representative = members.find((member) => member.is_representative);
  const confirmable = group.status === "active" && group.representative_job_id !== null;
  const send = (next: GroupWrite) => {
    if (!busy) {
      write.mutate(next);
    }
  };

  // a write that takes the focused control away (its member left the group): focus goes to the list
  const list = useRef<HTMLUListElement>(null);
  const lastFocused = useRef<Element | null>(null);
  useLayoutEffect(() => {
    const was = lastFocused.current;
    if (was !== null && !was.isConnected && focusDropped()) {
      lastFocused.current = null;
      list.current?.focus();
    }
  });

  const failed = write.error;
  return (
    <div className={styles.group} onFocus={(event) => { lastFocused.current = event.target; }}>
      {group.representative_job_id === null && group.unscorable_reason !== null && (
        <p className={styles.unscorable}>
          {t("card.not_scorable", { reason: serverText("group_unscorable", group.unscorable_reason) })}
        </p>
      )}
      <ul ref={list} role="list" tabIndex={-1} className={styles.list} aria-label={t("group.members")} aria-busy={busy}>
        {members.map((member) => (
          <Member key={member.job_id} member={member} electionReason={group.election_reason}
            thisJob={member.job_id === thisJobId} busy={busy} onOpen={onOpen} onWrite={send} />
        ))}
      </ul>
      {confirmable && (
        <div className={styles.confirm}>
          <button type="button" className={styles.button} disabled={busy} onClick={() => { setConfirming(true); }}>
            {t("group.confirm")}
          </button>
        </div>
      )}
      {failed !== null && retryable(failed) && (
        <div role="alert" className={styles.conflict}>
          <p>{t("group.conflict")}</p>
          <button type="button" className={styles.button} onClick={() => { send(write.variables); }}>
            {t("group.try_again")}
          </button>
        </div>
      )}
      {failed !== null && !retryable(failed) && <ProblemBox error={failed} />}
      <ConfirmDialog open={confirming} title={t("group.confirm_title")} confirmLabel={t("group.confirm")} busy={busy}
        onCancel={() => { setConfirming(false); }}
        onConfirm={() => {
          write.mutate({ action: "confirm" }, { onSettled: () => { setConfirming(false); } });
        }}>
        {t("group.confirm_body", { title: representative?.title ?? "" })}
      </ConfirmDialog>
    </div>
  );
}
