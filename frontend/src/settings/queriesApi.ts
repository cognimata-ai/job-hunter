// The data of Queries & sources (spec §5.5.1, §14.2, §15.7; plan M4 Task 29, resolutions b and c): the live
// queries and their planned cells, adding a query (the section's own and a proposal's item), and the query
// proposal of the active profile.
//
// - `GET /api/queries` and `GET /api/queries/stats` (`QUERIES_KEY`, the stats under it): the stats are read once
//   for the section, when a row's cells are first shown, and filtered by query there.
// - `POST /api/queries` creates a query that is not enabled (its route's docstring): `useAddQuery` then adds it
//   to the draft enabled (`addEntry`); the save is the section's panel's. A 409 `conflict_state` with a
//   `query_id` is a live query of the same text: the draft gains it the same way when it lacks it.
// - The proposal shown is the profile's `latest_proposal` (so a reload shows it again, D18) or the one the keyed
//   `POST /api/profile/query-proposals` answered; it is read under `PROFILE_KEY` and polled every 3 s with
//   `X-JH-Background: 1` while `running`. Its end reads the profile again.
// - Only the proposal's POST takes an `Idempotency-Key` (§14.1); none of the query writes does. No write is
//   retried by itself, and every failure is a `ProblemError`.
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { apiFetch, type ProblemError, toProblem } from "../api/client";
import { useKeyedMutation } from "../api/idempotency";
import type { components } from "../api/schema";
import { PROFILE_KEY } from "../profile/api";
import { useSettingsForm } from "./useSettingsForm";

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

async function send<T>(path: string, init: Parameters<typeof apiFetch>[1]): Promise<T> {
  try {
    return await apiFetch<T>(path, init);
  } catch (error) {
    throw toProblem(error);
  }
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
    queryFn: ({ signal }) => send<QueryListOut[]>("/api/queries", { signal }),
  });
}

/** Every planned cell of a live query, read once `enabled` (a row's cells shown). */
export function useQueryStats(enabled: boolean): UseQueryResult<CellStatsOut[], ProblemError> {
  return useQuery<CellStatsOut[], ProblemError>({
    queryKey: QUERY_STATS_KEY,
    queryFn: ({ signal }) => send<CellStatsOut[]>("/api/queries/stats", { signal }),
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
  add: (vars: AddQueryVars, onDone?: (outcome: AddOutcome) => void) => void;
  pending: boolean;
  /** The last outcome, until the next add. */
  outcome: AddOutcome | null;
  /** The last add's failure (not a 409 with a `query_id`, which is an outcome). */
  error: ProblemError | null;
}

/**
 * `POST /api/queries`, then the query enters the draft enabled (module docstring); the queries are read again.
 * The draft is the form's when the answer arrives, so an entry the user added meanwhile counts.
 */
export function useAddQuery(): AddQuery {
  const form = useSettingsForm();
  const queryClient = useQueryClient();
  const latest = useRef(form);
  useEffect(() => {
    latest.current = form;
  });
  const [outcome, setOutcome] = useState<AddOutcome | null>(null);
  const mutation = useMutation<QueryOut, ProblemError, AddQueryVars>({
    mutationFn: (vars) => send<QueryOut>("/api/queries", { method: "POST", body: JSON.stringify(vars) }),
    retry: false,
  });
  const done = (next: AddOutcome, onDone?: (outcome: AddOutcome) => void) => {
    setOutcome(next);
    void queryClient.invalidateQueries({ queryKey: QUERIES_KEY });
    onDone?.(next);
  };
  return {
    add: (vars, onDone) => {
      setOutcome(null);
      mutation.mutate(vars, {
        onSuccess: (out) => {
          latest.current.addEntry("queries", out.id);
          done({ kind: "added", id: out.id, text: out.text }, onDone);
        },
        onError: (error) => {
          const id = conflictId(error, "query_id");
          if (id === null) {
            return;
          }
          const held = latest.current.draft.queries.some((entry) => entry.query_id === id);
          if (!held) {
            latest.current.addEntry("queries", id);
          }
          done({ kind: held ? "exists" : "enabled", id, text: vars.text.trim() }, onDone);
        },
      });
    },
    pending: mutation.isPending,
    outcome,
    error: mutation.error !== null && conflictId(mutation.error, "query_id") === null ? mutation.error : null,
  };
}

/** `PATCH /api/queries/{id} {text, lang}`: the query retired and its new row answered (its route's docstring). */
export function useEditQuery(id: number): UseMutationResult<QueryOut, ProblemError, { text: string; lang: QueryLang }> {
  return useMutation<QueryOut, ProblemError, { text: string; lang: QueryLang }>({
    mutationFn: (vars) => send<QueryOut>(`/api/queries/${String(id)}`, { method: "PATCH", body: JSON.stringify(vars) }),
    retry: false,
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
      return send<ProposalOut>(`/api/profile/query-proposals/${String(id)}`, { signal, background: known?.status === "running" });
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
