// The Contested tab (spec §15.4, §11.10 item 7; plan M4 Task 24, resolution f, Ruling T6-d): the critic's rounds
// in order (each with its triggers, the returned verdict, the derived outcome and its issues per criterion), the
// rejected drafts round by round as a level diff against the final draft, and the nulled criteria. A result that
// is not contested says so. An issue names an LLM criterion, or a required fact by its id (the server's,
// verbatim in a <code>).
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { EmptyState } from "../components/EmptyState";
import { useServerText } from "../i18n/index";
import styles from "./ContestedTab.module.css";
import type { ContestedOut } from "./detailApi";
import { TabSection } from "./TabSection";

type RoundOut = ContestedOut["rounds"][number];
type RejectedDraftOut = ContestedOut["rejected_drafts"][number];

/** A criterion's name, or a fact's id as the server sends it. */
function Subject({ code }: { code: string }) {
  const { i18n } = useTranslation();
  const serverText = useServerText();
  return i18n.exists(`criterion.${code}`) ? <>{serverText("criterion", code)}</> : <code className={styles.code}>{code}</code>;
}

function Round({ round }: { round: RoundOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const id = useId();
  return (
    <article aria-labelledby={id} className={styles.round}>
      <h4 id={id} className={styles.subheading}>{t("contested.round", { round: String(round.round) })}</h4>
      <p>
        <span className={styles.label}>{t("contested.triggers")}: </span>
        {round.triggers.map((trigger) => serverText("critic_trigger", trigger)).join(", ")}
      </p>
      <p>
        <span className={styles.label}>{t("contested.verdict")}: </span>
        {serverText("critic_verdict", round.verdict)} ·{" "}
        <span className={styles.label}>{t("contested.outcome")}: </span>
        {serverText("round_outcome", round.outcome)}
      </p>
      {round.issues.length === 0 ? (
        <p className={styles.muted}>{t("contested.no_issues")}</p>
      ) : (
        <ul className={styles.list} aria-label={t("contested.issues")}>
          {round.issues.map((issue, i) => (
            <li key={i}>
              <Subject code={issue.criterion} />: {serverText("critic_issue", issue.issue)}
              {issue.suggested_level !== null && ` (${t("contested.suggested", { level: String(issue.suggested_level) })})`}
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}

function Draft({ draft }: { draft: RejectedDraftOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const id = useId();
  const none = t("drawer.no_value");
  const level = (value: number | null) => (value === null ? none : String(value));
  return (
    <li aria-labelledby={id} className={styles.draft}>
      <h4 id={id} className={styles.subheading}>{t("contested.draft", { round: String(draft.round) })}</h4>
      {draft.level_diff.length === 0 ? (
        <p className={styles.muted}>{t("contested.same_levels")}</p>
      ) : (
        <ul className={styles.list}>
          {draft.level_diff.map((diff) => (
            <li key={diff.criterion}>
              {serverText("criterion", diff.criterion)}: {level(diff.from_level)} → {level(diff.to_level)}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function ContestedTab({ contested }: { contested: ContestedOut | null }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  if (contested === null) {
    return <EmptyState title={t("contested.none")} />;
  }
  return (
    <div className={styles.tab}>
      <TabSection heading={t("contested.rounds")}>
        {contested.rounds.map((round) => <Round key={round.round} round={round} />)}
      </TabSection>
      <TabSection heading={t("contested.rejected")}>
        {contested.rejected_drafts.length === 0 ? (
          <p className={styles.muted}>{t("drawer.none")}</p>
        ) : (
          <ul role="list" className={styles.drafts}>
            {contested.rejected_drafts.map((draft) => <Draft key={draft.round} draft={draft} />)}
          </ul>
        )}
      </TabSection>
      <TabSection heading={t("contested.nulled")}>
        {contested.nulled_criteria.length === 0 ? (
          <p className={styles.muted}>{t("drawer.none")}</p>
        ) : (
          <ul className={styles.list}>
            {contested.nulled_criteria.map((criterion) => <li key={criterion}>{serverText("criterion", criterion)}</li>)}
          </ul>
        )}
      </TabSection>
    </div>
  );
}
