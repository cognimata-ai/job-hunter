// The data of Queries & sources (spec §5.5.1, §14.2, §15.7; plan M4 Task 29, resolutions b and c): the live
// queries and their planned cells, adding a query (the section's own and a proposal's item), and the query
// proposal of the active profile.
//
// - `GET /api/queries` and `GET /api/queries/stats` (`QUERIES_KEY`, the stats under it): the stats are read once
//   for the section, when a row's cells are first shown, and filtered by query there.
// - `POST /api/queries` creates a query that is not enabled (its route's docstring): `useAddQuery` then adds it
//   to the draft enabled (`addEntry`); the save is the section's panel's. A 409 `conflict_state` with a
//   `query_id` is a live query of the same text: the draft gains it the same way when it lacks it.
// - A committed write's follow-up (the draft, the rebase, the list read again) is its mutation's own
//   `onSuccess`, which runs whether or not the component that sent it is still there (Task 28's save, M2). It
//   reads the draft as it is when the answer comes (`useLatestForm`), so a change made in flight wins.
// - The proposal shown is the profile's `latest_proposal` (so a reload shows it again, D18) or the one the keyed
//   `POST /api/profile/query-proposals` answered; it is read under `PROFILE_KEY` and polled every 3 s with
//   `X-JH-Background: 1` while `running`. Its end reads the profile again.
// - Only the proposal's POST takes an `Idempotency-Key` (§14.1); none of the query writes does. No write is
//   retried by itself, and every failure is a `ProblemError`.
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { apiFetch, apiSend, type ProblemError, toProblem } from "../api/client";
import { useKeyedMutation } from "../api/idempotency";
import type { components } from "../api/schema";
import { PROFILE_KEY } from "../profile/api";
import { type SettingsForm, useSettingsForm } from "./useSettingsForm";

export type QueryListOut = components["schemas"]["QueryListOut"];
export type CellStatsOut = components["schemas"]["CellStatsOut"];
export type QueryOut = components["schemas"]["QueryOut"];
export type QueryLang = QueryOut["lang"];
export type ProposalOut = components["schemas"]["ProposalOut"];
export type ProposedQueryOut = components["schemas"]["ProposedQueryOut"];
export type ProposalCreatedOut = components["schemas"]["ProposalCreatedOut"];

export const QUERIES_KEY = ["queries"] as const;
export const QUERY_STATS_KEY = [...QUERIES_KEY, "stats"] as const;
export const proposalKey = (id: number) => [...PROFILE_KEY, "query-proposal", id] as const;
/** How often a running proposal is read again. */
export const PROPOSAL_POLL_MS = 3000;
/** A query's text: 2 to 120 characters once trimmed (`queries.proposals.TEXT_CHARS`, `authoring.create_query`). */
export const QUERY_TEXT_CHARS = [2, 120] as const;
/** The languages a query is in (`QueryIn.lang`), held to the schema both ways. */
export const QUERY_LANGS = Object.keys({ de: true, en: true } satisfies Record<QueryLang, true>) as QueryLang[];

/** The mutation keys of a query added from a proposal's item, and of any other add. */
export const ADD_PROPOSED_KEY = [...QUERIES_KEY, "add", "proposed"] as const;
export const ADD_QUERY_KEY = [...QUERIES_KEY, "add"] as const;

/** The form of the caller's last render while it is mounted, `null` once it is not: a write's follow-up reads
 * the draft as it is when its answer comes. */
export function useLatestForm(): { readonly current: SettingsForm | null } {
  const form = useSettingsForm();
  const latest = useRef<SettingsForm | null>(form);
  useEffect(() => {
    latest.current = form;
  });
  useEffect(() => () => {
    latest.current = null;
  }, []);
  return latest;
}

/** A query's text as the server counts it: its code points once trimmed. */
export function queryTextLength(text: string): number {
  return Array.from(text.trim()).length;
}

export function queryTextValid(text: string): boolean {
  const length = queryTextLength(text);
  return length >= QUERY_TEXT_CHARS[0] && length <= QUERY_TEXT_CHARS[1];
}

/** The id a 409 `conflict_state` names in `member` (`query_id`, `watchlist_company_id`), else `null`. */
export function conflictId(error: ProblemError, member: string): number | null {
  const id = error.extra[member];
  return error.status === 409 && error.code === "conflict_state" && typeof id === "number" ? id : null;
}

/** The live queries by id. */
export function useQueries(): UseQueryResult<QueryListOut[], ProblemError> {
  return useQuery<QueryListOut[], ProblemError>({
    queryKey: QUERIES_KEY,
    queryFn: ({ signal }) => apiSend<QueryListOut[]>("/api/queries", { signal }),
  });
}

/** Every planned cell of a live query, read once `enabled` (a row's cells shown). */
export function useQueryStats(enabled: boolean): UseQueryResult<CellStatsOut[], ProblemError> {
  return useQuery<CellStatsOut[], ProblemError>({
    queryKey: QUERY_STATS_KEY,
    queryFn: ({ signal }) => apiSend<CellStatsOut[]>("/api/queries/stats", { signal }),
    enabled,
  });
}

/** What adding a query came to: created and added (`added`), a live query of that text added (`enabled`), or
 * one the draft holds already (`exists`). */
export type AddOutcome = { kind: "added" | "enabled" | "exists"; id: number; text: string };

export interface AddQueryVars {
  text: string;
  lang: QueryLang;
  /** A proposal's item: its proposal and its exploration flag (`origin` becomes `llm_proposal`). */
  proposal_id?: number;
  exploration?: boolean;
}

export interface AddQuery {
  /** Sends the add; `onDone` runs with its outcome while the caller is mounted (it clears a field). */
  add: (vars: AddQueryVars, onDone?: (outcome: AddOutcome) => void) => void;
  pending: boolean;
  /** The last outcome, until the next add. */
  outcome: AddOutcome | null;
  /** The last add's failure (not a 409 with a `query_id`, which is an outcome). */
  error: ProblemError | null;
}

/**
 * `POST /api/queries`, then the query enters the draft enabled (module docstring) and the queries are read
 * again, in the mutation's own `onSuccess`. Whether the draft held a 409's query is read when the answer
 * comes, so an entry the user added meanwhile counts. `proposed` keys an add from a proposal's item
 * (`ADD_PROPOSED_KEY`), so the panel can wait for it.
 */
export function useAddQuery({ proposed = false }: { proposed?: boolean } = {}): AddQuery {
  const form = useSettingsForm();
  const latest = useLatestForm();
  const queryClient = useQueryClient();
  const mutation = useMutation<AddOutcome, ProblemError, AddQueryVars>({
    mutationKey: proposed ? ADD_PROPOSED_KEY : ADD_QUERY_KEY,
    mutationFn: async (vars) => {
      try {
        const out = await apiSend<QueryOut>("/api/queries", { method: "POST", body: JSON.stringify(vars) });
        return { kind: "added", id: out.id, text: out.text };
      } catch (error) {
        const problem = toProblem(error);
        const id = conflictId(problem, "query_id");
        if (id === null) {
          throw problem;
        }
        const held = (latest.current ?? form).draft.queries.some((entry) => entry.query_id === id);
        return { kind: held ? "exists" : "enabled", id, text: vars.text.trim() };
      }
    },
    retry: false,
    onSuccess: async (outcome) => {
      if (outcome.kind !== "exists") {
        (latest.current ?? form).addEntry("queries", outcome.id);
      }
      await queryClient.invalidateQueries({ queryKey: QUERIES_KEY });
    },
  });
  return {
    add: (vars, onDone) => {
      mutation.mutate(vars, { onSuccess: (outcome) => { onDone?.(outcome); } });
    },
    pending: mutation.isPending,
    outcome: mutation.data ?? null,
    error: mutation.error,
  };
}

/** An edit as sent (`PATCH /api/queries/{id} {text, lang}`), with the query's draft entry when it was sent. */
export interface EditVars {
  id: number;
  text: string;
  lang: QueryLang;
  /** The draft's entry of the query at submit: the follow-up's fallback once the section is gone. */
  entry: { enabled: boolean } | null;
}

/**
 * `PATCH /api/queries/{id}`: the query retired and its new row answered (its route's docstring), then, in the
 * mutation's own `onSuccess` (it runs also once the editor is closed): the rebase (the route saved the settings
 * with the new id itself), the query's draft entry moved to the new id (Ruling T29-a's extension: a toggle, or
 * an entry the draft had added, which no settings version names, so the server moved nothing), and the list
 * read again. The entry is the draft's when the answer comes (a toggle made in flight wins), the one captured
 * at submit once the section is gone. A failed rebase moves nothing (the moves need the version the route
 * saved), but for an entry the draft had added: no settings version names it, so the route saved none and the
 * base is current, and the retired id must not stay in the draft (the dry-run refuses it, Ruling T29-c).
 */
export function useEditQuery(): UseMutationResult<QueryOut, ProblemError, EditVars> {
  const form = useSettingsForm();
  const latest = useLatestForm();
  const queryClient = useQueryClient();
  return useMutation<QueryOut, ProblemError, EditVars>({
    mutationFn: ({ id, text, lang }) =>
      apiSend<QueryOut>(`/api/queries/${String(id)}`, { method: "PATCH", body: JSON.stringify({ text, lang }) }),
    retry: false,
    onSuccess: async (out, vars) => {
      const now = latest.current;
      const target = now ?? form; // its methods dispatch to the page's form
      const entry = now === null ? vars.entry : (now.draft.queries.find((candidate) => candidate.query_id === vars.id) ?? null);
      const saved = target.base.payload.queries.some((candidate) => candidate.query_id === vars.id);
      const rebased = await target.rebase();
      if ((rebased || !saved) && out.id !== vars.id && entry !== null) {
        target.removeEntry("queries", vars.id); // an entry the draft had added: no settings version moved it
        target.addEntry("queries", out.id);
        target.setEntry("queries", out.id, entry.enabled);
      }
      await queryClient.invalidateQueries({ queryKey: QUERIES_KEY });
    },
  });
}

/** Proposal `id` (`GET /api/profile/query-proposals/{id}`), none for `null`; read every 3 s while running, a read
 * of a proposal known to be running being a poll. Its end reads the profile again (its `latest_proposal`). */
export function useProposal(id: number | null): UseQueryResult<ProposalOut, ProblemError> {
  const queryClient = useQueryClient();
  const proposal = useQuery<ProposalOut, ProblemError>({
    queryKey: proposalKey(id ?? 0),
    queryFn: ({ signal }) => {
      const known = queryClient.getQueryData<ProposalOut>(proposalKey(id ?? 0));
      return apiSend<ProposalOut>(`/api/profile/query-proposals/${String(id)}`, { signal, background: known?.status === "running" });
    },
    enabled: id !== null,
    refetchInterval: (query) => (query.state.data?.status === "running" ? PROPOSAL_POLL_MS : false),
  });
  const status = proposal.data?.status;
  const running = useRef(false);
  useEffect(() => {
    if (status === "running") {
      running.current = true;
    } else if (status !== undefined && running.current) {
      running.current = false;
      void queryClient.invalidateQueries({ queryKey: PROFILE_KEY, exact: true });
    }
  }, [status, queryClient]);
  return proposal;
}

/** Ask for a proposal of the active profile (`POST /api/profile/query-proposals`, keyed): 202 `{proposal_id}`
 * (the running one when there is one), and the profile is read again. */
export function useRequestProposal(): UseMutationResult<ProposalCreatedOut, ProblemError, undefined> {
  const queryClient = useQueryClient();
  return useKeyedMutation(
    (_vars: undefined, key) =>
      apiFetch<ProposalCreatedOut>("/api/profile/query-proposals", { method: "POST", idempotencyKey: key }),
    { onSuccess: () => queryClient.invalidateQueries({ queryKey: PROFILE_KEY, exact: true }) },
  );
}
