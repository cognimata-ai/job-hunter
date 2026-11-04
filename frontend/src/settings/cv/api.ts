// The CV & profile section's data (spec §12, §14.2, §15.7; plan M4 Task 27, resolutions a and b).
//
// - The draft in progress is `useProfile().open_draft` (a reload finds it again there); `useDraft(id)` reads it
//   under `PROFILE_KEY` (`["profile", "draft", id]`), so a write that invalidates the profile reads it again too.
//   While the server works on it (`uploaded`, `extracting_text`, `extracting_profile`) it is polled every 3 s
//   with `X-JH-Background: 1` (D28), and not in `text_review`, `ready` or `failed`, where it is the user's turn.
// - The upload and the paste take no `Idempotency-Key` (their routes take none; a repeat opens a second draft,
//   which discards the first: Ruling T27-c); the text approval, the retry and the confirm are keyed (D29).
// - Every write's failure is a `ProblemError` (`toProblem`); none is retried by itself.
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useCallback, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import { apiFetch, type ProblemError, toProblem } from "../../api/client";
import { useKeyedMutation } from "../../api/idempotency";
import type { components } from "../../api/schema";
import { SETTINGS_KEY, type SettingsOut } from "../../app/AppShell";
import { useToast } from "../../components/Toaster";
import { JOBS_KEY } from "../../jobs/api";
import { PROFILE_KEY, type ProfileListOut } from "../../profile/api";
import { STATUS_KEY } from "../../system/api";
import type { SpanEdit } from "./segments";

export type DraftOut = components["schemas"]["DraftOut"];
export type DraftStatusCode = DraftOut["status"];
export type DraftRepair = DraftOut["repairs"][number];
export type ConfirmedProfile = components["schemas"]["ConfirmedProfile"];
export type ValidationIssueOut = components["schemas"]["ValidationIssueOut"];
export type ValidationIssuesOut = components["schemas"]["ValidationIssuesOut"];
export type DraftCreatedOut = components["schemas"]["DraftCreatedOut"];
export type RescoreEstimateOut = components["schemas"]["RescoreEstimateOut"];
export type ConfirmOut = components["schemas"]["ConfirmOut"];
export type ExtractingOut = components["schemas"]["ExtractingOut"];
/** A retry's body (`RetryIn`): the same input again, or the original PDF with its disclosure acknowledged; the
 * acknowledgement's default (`false`) is left out of an `llm` retry. */
export type RetryVars = { mode: "llm" } | { mode: "native_pdf"; ack_native_pdf: true };
export type StrippedTextOut = components["schemas"]["StrippedTextOut"];

/** How often a draft the server works on is read again. */
export const DRAFT_POLL_MS = 3000;
/** The upload's and the paste's mutation key: the section shows "Uploading" while one runs. */
export const CV_UPLOAD_KEY = [...PROFILE_KEY, "cv"] as const;
/** The paste's cap: its JSON body, in UTF-8 bytes (`upload.PASTE_BODY_MAX_BYTES`). */
export const PASTE_MAX_BYTES = 2 ** 20;

export const draftKey = (id: number) => [...PROFILE_KEY, "draft", id] as const;
export const estimateKey = (id: number) => [...PROFILE_KEY, "rescore-estimate", id] as const;

const PROCESSING: ReadonlySet<DraftStatusCode> = new Set(["uploaded", "extracting_text", "extracting_profile"]);
const REVIEWABLE: ReadonlySet<DraftStatusCode> = new Set(["text_review", "ready"]);

/** Whether the server works on a draft in `status` (it is polled), rather than waiting for the user. */
export function isProcessing(status: DraftStatusCode | undefined): boolean {
  return status !== undefined && PROCESSING.has(status);
}

/** Whether a draft in `status` is in the user's review: a new upload or paste discards it (D18). */
export function isInReview(status: DraftStatusCode | undefined): boolean {
  return status !== undefined && REVIEWABLE.has(status);
}

/** Whether a draft in `status` is over: confirmed or discarded. */
export function isOver(status: DraftStatusCode | undefined): boolean {
  return status === "confirmed" || status === "discarded";
}

async function send<T>(path: string, init: Parameters<typeof apiFetch>[1]): Promise<T> {
  try {
    return await apiFetch<T>(path, init);
  } catch (error) {
    throw toProblem(error);
  }
}

/**
 * Draft `id` (`GET /api/profile/drafts/{id}`), none for `null`. Read every 3 s while the server works on it; a
 * read of a draft known to be in such a state is a poll (`X-JH-Background: 1`), the first read and a read after
 * the user's own write are not.
 */
export function useDraft(id: number | null): UseQueryResult<DraftOut, ProblemError> {
  const queryClient = useQueryClient();
  return useQuery<DraftOut, ProblemError>({
    queryKey: draftKey(id ?? 0),
    queryFn: ({ signal }) => {
      const known = queryClient.getQueryData<DraftOut>(draftKey(id ?? 0));
      return send<DraftOut>(`/api/profile/drafts/${String(id)}`, { signal, background: isProcessing(known?.status) });
    },
    enabled: id !== null,
    refetchInterval: (query) => (isProcessing(query.state.data?.status) ? DRAFT_POLL_MS : false),
  });
}

/**
 * The model of the active settings' `extract_profile` node, shown verbatim (it is data): read from the settings
 * the shell has read (`SETTINGS_KEY`), never through a second query of it. `undefined` until they are read.
 */
export function useExtractModel(): string | undefined {
  const queryClient = useQueryClient();
  const subscribe = useCallback((onChange: () => void) => queryClient.getQueryCache().subscribe(onChange), [queryClient]);
  return useSyncExternalStore(
    subscribe,
    () => queryClient.getQueryData<SettingsOut>(SETTINGS_KEY)?.payload.scoring.model_policy.nodes.extract_profile.model,
  );
}

export interface UploadVars {
  file: File;
  /** Send the original PDF to the model instead of its text (§12.7). */
  nativePdf: boolean;
}

/** The CV upload (`POST /api/profile/cv`, multipart `file` and `native_pdf_opt_in`): 202 `{draft_id}`. */
export function useUploadCv(): UseMutationResult<DraftCreatedOut, ProblemError, UploadVars> {
  const queryClient = useQueryClient();
  return useMutation<DraftCreatedOut, ProblemError, UploadVars>({
    mutationKey: CV_UPLOAD_KEY,
    mutationFn: ({ file, nativePdf }) => {
      const form = new FormData();
      form.append("file", file);
      if (nativePdf) {
        form.append("native_pdf_opt_in", "true");
      }
      return send<DraftCreatedOut>("/api/profile/cv", { method: "POST", body: form });
    },
    retry: false,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: PROFILE_KEY, exact: true });
    },
  });
}

/** The CV paste (`POST /api/profile/cv-text {text}`): 202 `{draft_id}`. */
export function usePasteCv(): UseMutationResult<DraftCreatedOut, ProblemError, string> {
  const queryClient = useQueryClient();
  return useMutation<DraftCreatedOut, ProblemError, string>({
    mutationKey: CV_UPLOAD_KEY,
    mutationFn: (text) => send<DraftCreatedOut>("/api/profile/cv-text", { method: "POST", body: JSON.stringify({ text }) }),
    retry: false,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: PROFILE_KEY, exact: true });
    },
  });
}

/** The draft's complete edit set (`PUT …/text {span_edits}`); the draft is read again after it, since the
 * answer holds only the stripped text and the next selection maps through the fresh map. */
export function useEditText(id: number): UseMutationResult<StrippedTextOut, ProblemError, SpanEdit[]> {
  const queryClient = useQueryClient();
  return useMutation<StrippedTextOut, ProblemError, SpanEdit[]>({
    mutationFn: (spanEdits) =>
      send<StrippedTextOut>(`/api/profile/drafts/${String(id)}/text`, {
        method: "PUT",
        body: JSON.stringify({ span_edits: spanEdits }),
      }),
    retry: false,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: draftKey(id) }),
  });
}

/** Approve the reviewed text (`POST …/text-review`, keyed): 202, and the draft is polled again. */
export function useApproveText(id: number): UseMutationResult<ExtractingOut, ProblemError, undefined> {
  const queryClient = useQueryClient();
  return useKeyedMutation(
    (_vars: undefined, key) =>
      apiFetch<ExtractingOut>(`/api/profile/drafts/${String(id)}/text-review`, { method: "POST", idempotencyKey: key }),
    { onSuccess: () => queryClient.invalidateQueries({ queryKey: draftKey(id) }) },
  );
}

/** A failed draft's repair (`POST …/retry {mode, ack_native_pdf}`, keyed): 202, and the draft is polled again. */
export function useRetryDraft(id: number): UseMutationResult<ExtractingOut, ProblemError, RetryVars> {
  const queryClient = useQueryClient();
  return useKeyedMutation(
    (vars: RetryVars, key) =>
      apiFetch<ExtractingOut>(`/api/profile/drafts/${String(id)}/retry`, {
        method: "POST",
        body: JSON.stringify(vars),
        idempotencyKey: key,
      }),
    { onSuccess: () => queryClient.invalidateQueries({ queryKey: draftKey(id) }) },
  );
}

/** Save the edited profile of a ready draft (`PUT /drafts/{id} {draft}`): its issues; the draft is read again
 * (the server stores the profile repaired). */
export function useSaveDraft(id: number): UseMutationResult<ValidationIssuesOut, ProblemError, ConfirmedProfile> {
  const queryClient = useQueryClient();
  return useMutation<ValidationIssuesOut, ProblemError, ConfirmedProfile>({
    mutationFn: (draft) =>
      send<ValidationIssuesOut>(`/api/profile/drafts/${String(id)}`, { method: "PUT", body: JSON.stringify({ draft }) }),
    retry: false,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: draftKey(id) }),
  });
}

/** Discard a draft in review, ready or failed (`POST …/discard`, 204); the profile is read again. */
export function useDiscardDraft(id: number): UseMutationResult<undefined, ProblemError, undefined> {
  const queryClient = useQueryClient();
  return useMutation<undefined, ProblemError, undefined>({
    mutationFn: () => send<undefined>(`/api/profile/drafts/${String(id)}/discard`, { method: "POST" }),
    retry: false,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PROFILE_KEY }),
  });
}

/** What confirming draft `id` would re-score and cost (`GET …/rescore-estimate`), read when the modal opens. */
export function useRescoreEstimate(id: number): UseQueryResult<RescoreEstimateOut, ProblemError> {
  return useQuery<RescoreEstimateOut, ProblemError>({
    queryKey: estimateKey(id),
    queryFn: ({ signal }) => send<RescoreEstimateOut>(`/api/profile/drafts/${String(id)}/rescore-estimate`, { signal }),
    staleTime: 0,
    gcTime: 0,
  });
}

/**
 * Confirm draft `id` and activate its profile (`POST …/confirm {expected_rescore_ack: true}`, keyed): 201, and
 * the notice "Profile v<n> confirmed". The profile (the draft with it), the settings (the active tuple), the
 * header's status and every Jobs list are read again, since every card's score now waits on the re-score; the
 * new version's number is read from the profile (`fallbackVersionNo`, the estimate's, until it is there). The
 * notice is the mutation's own callback: the section leaves the review once the draft reads `confirmed`.
 */
export function useConfirmDraft(
  id: number,
  fallbackVersionNo: number | undefined,
): UseMutationResult<ConfirmOut, ProblemError, undefined> {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  return useKeyedMutation(
    (_vars: undefined, key) =>
      apiFetch<ConfirmOut>(`/api/profile/drafts/${String(id)}/confirm`, {
        method: "POST",
        body: JSON.stringify({ expected_rescore_ack: true }),
        idempotencyKey: key,
      }),
    {
      onSuccess: async (out) => {
        void queryClient.invalidateQueries({ queryKey: SETTINGS_KEY });
        void queryClient.invalidateQueries({ queryKey: STATUS_KEY });
        void queryClient.invalidateQueries({ queryKey: JOBS_KEY });
        await queryClient.invalidateQueries({ queryKey: PROFILE_KEY });
        const list = queryClient.getQueryData<ProfileListOut>(PROFILE_KEY);
        const versionNo = list?.versions.find((v) => v.id === out.profile_version_id)?.version_no ?? fallbackVersionNo;
        toast({ message: versionNo === undefined ? t("cv.confirm.done_plain") : t("cv.confirm.done", { version: versionNo }) });
      },
    },
  );
}
