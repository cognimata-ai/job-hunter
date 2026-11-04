// The query proposal of Queries & sources (spec §5.5.1, §11.12, §15.7; plan M4 Task 29, resolution c; D18).
//
// - [Propose queries (~$x)] sends the keyed `POST /api/profile/query-proposals` (its cost the profile's
//   `estimates.propose_queries_usd`). It is disabled without an active profile ("Confirm a profile first") and
//   while the proposal shown is running; a refusal (409 `conflict_state`) shows its detail.
// - **The proposal shown** is the profile's `latest_proposal`, or the newer one the POST answered, so a reload
//   shows it again and never asks for a second paid one. It is read every 3 s while running (`useProposal`).
// - `ready`: its queries, each with its text (in its language), its language, its target title, its exploration
//   flag and the model's rationale, all as text, and [Add] (`POST /api/queries` with the proposal and the flag;
//   the query enters the draft enabled). An item is matched against the live queries by its text alone (case
//   folded), as the server's `lower(text)` uniqueness is blind to the language: a match in its own language
//   that the draft holds (or the query its own Add answered) reads "Added"; a match in the other language reads
//   "Already a query (<language>)", with no [Add], since an add could only meet that query. While an item's Add
//   is out, [Propose queries] waits, so a new proposal never replaces the item under it.
// - `failed`: why, through `proposal_failure`; a code without a text of its own reads "The proposal failed"
//   with the code. `purged`: the proposal went with its profile.
import { useIsMutating } from "@tanstack/react-query";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { Spinner } from "../components/Spinner";
import i18n, { useServerText } from "../i18n/index";
import { useProfile } from "../profile/api";
import { useFormat } from "../system/format";
import fields from "./fields.module.css";
import styles from "./ProposalPanel.module.css";
import {
  ADD_PROPOSED_KEY,
  type ProposalOut,
  type ProposedQueryOut,
  type QueryListOut,
  useAddQuery,
  useProposal,
  useQueries,
  useRequestProposal,
} from "./queriesApi";
import { useSettingsForm } from "./useSettingsForm";

/** The newer of two proposal ids (ids are serial). */
function newest(a: number | null, b: number | null): number | null {
  if (a === null) {
    return b;
  }
  return b === null ? a : Math.max(a, b);
}

function Item({ item, proposalId, queries }: { item: ProposedQueryOut; proposalId: number; queries: QueryListOut[] | undefined }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const form = useSettingsForm();
  const adding = useAddQuery({ proposed: true });
  const textId = useId();
  const held = (id: number) => form.draft.queries.some((entry) => entry.query_id === id);
  const key = item.text.trim().toLowerCase();
  const match = queries?.find((query) => query.text.trim().toLowerCase() === key);
  const other = match !== undefined && match.lang !== item.lang ? match : undefined;
  const added = (match !== undefined && other === undefined && held(match.id)) || (adding.outcome !== null && held(adding.outcome.id));
  const outcome = adding.outcome?.kind;
  let state;
  if (added) {
    state = <span className={styles.added}>{t("settings_proposal.added")}</span>;
  } else if (other !== undefined) {
    state = <span className={styles.exists}>{t("settings_proposal.exists_other", { language: serverText("language_code", other.lang) })}</span>;
  } else {
    state = (
      <button type="button" className={fields.button} disabled={adding.pending} aria-busy={adding.pending || undefined}
        aria-label={t("settings_proposal.add_label", { text: item.text })}
        onClick={() => { adding.add({ text: item.text, lang: item.lang, proposal_id: proposalId, exploration: item.exploration }); }}>
        {t("settings_proposal.add")}
      </button>
    );
  }
  return (
    <li className={styles.item}>
      <div className={styles.itemHead}>
        <span id={textId} lang={item.lang} className={styles.itemText}>{item.text}</span>
        {state}
      </div>
      <p className={styles.meta}>
        <span>{serverText("language_code", item.lang)}</span>
        <span>{t("settings_proposal.target", { title: item.target_title })}</span>
        {item.exploration && <span className={styles.badge}>{t("settings_proposal.exploration")}</span>}
      </p>
      <p className={styles.rationale}>{item.rationale}</p>
      {(outcome === "enabled" || outcome === "exists") && (
        <p role="status" className={styles.notice}>
          {t(outcome === "enabled" ? "settings_queries.exists_enabled" : "settings_queries.exists")}
        </p>
      )}
      {adding.error !== null && <ProblemBox error={adding.error} />}
    </li>
  );
}

function Failure({ code }: { code: string | null }) {
  const { t } = useTranslation();
  const known = code !== null && i18n.exists(`proposal_failure.${code}`);
  return (
    <div className={styles.failure}>
      <p className={styles.failureTitle}>{t("settings_proposal.failed")}</p>
      {known ? (
        <p>{t(`proposal_failure.${code}` as "proposal_failure.refused")}</p>
      ) : (
        code !== null && <p><code className={styles.code}>{code}</code></p>
      )}
    </div>
  );
}

function Shown({ proposal, id }: { proposal: ReturnType<typeof useProposal>; id: number }) {
  const { t } = useTranslation();
  const queries = useQueries();
  if (proposal.isPending) {
    return <Skeleton variant="line" count={2} />;
  }
  if (proposal.isError) {
    return <ProblemBox error={proposal.error} onRetry={() => { void proposal.refetch(); }} />;
  }
  const data: ProposalOut = proposal.data;
  switch (data.status) {
    case "running":
      return <Spinner label={t("settings_proposal.running")} />;
    case "failed":
      return <Failure code={data.failure_code} />;
    case "purged":
      return <p className={fields.hint}>{t("settings_proposal.purged")}</p>;
    case "ready": {
      const items = data.items ?? [];
      return (
        <>
          <p role="status" className={styles.count}>{t("settings_proposal.ready", { count: items.length })}</p>
          {items.length > 0 && (
            <ul role="list" className={styles.items}>
              {items.map((item, index) => (
                <Item key={`${String(index)}-${item.text}`} item={item} proposalId={id} queries={queries.data} />
              ))}
            </ul>
          )}
        </>
      );
    }
  }
}

export function ProposalPanel() {
  const { t } = useTranslation();
  const format = useFormat();
  const profile = useProfile();
  const request = useRequestProposal();
  const [requested, setRequested] = useState<number | null>(null);
  const headingId = useId();
  const reasonId = useId();
  const latest = profile.data?.latest_proposal ?? null;
  const shownId = newest(requested, latest?.id ?? null);
  const proposal = useProposal(shownId);
  const adding = useIsMutating({ mutationKey: ADD_PROPOSED_KEY }) > 0;
  let body;
  if (profile.isPending) {
    body = <Skeleton variant="line" count={2} />;
  } else if (profile.isError) {
    body = <ProblemBox error={profile.error} onRetry={() => { void profile.refetch(); }} />;
  } else {
    const active = profile.data.active !== null;
    // running: as the proposal reads; until it is read, while its first read is out or the profile says so
    const running = proposal.data !== undefined
      ? proposal.data.status === "running"
      : shownId !== null && (proposal.isFetching || latest?.status === "running");
    body = (
      <>
        <div className={styles.action}>
          <button type="button" className={fields.primary} disabled={!active || running || adding || request.isPending}
            aria-busy={request.isPending || undefined} aria-describedby={active ? undefined : reasonId}
            onClick={() => { request.mutate(undefined, { onSuccess: (out) => { setRequested(out.proposal_id); } }); }}>
            {t("settings_proposal.propose", { usd: format.usd(profile.data.estimates.propose_queries_usd) })}
          </button>
          {!active && <p id={reasonId} className={fields.hint}>{t("settings_proposal.no_profile")}</p>}
        </div>
        {request.error !== null && <ProblemBox error={request.error} />}
        {shownId === null ? <p className={fields.hint}>{t("settings_proposal.none")}</p> : <Shown proposal={proposal} id={shownId} />}
      </>
    );
  }
  return (
    <section aria-labelledby={headingId} className={styles.panel}>
      <h3 id={headingId} className={fields.heading}>{t("settings_proposal.heading")}</h3>
      {body}
    </section>
  );
}
