// A card of the Jobs list (spec §15.3, §15.9; plan M4 Task 22, Ruling j): the title (a heading that names the
// card), the company, the places and the work mode, the posted date (`postedLabel`), first seen and, when not
// today, last seen; the source chips (product names from the `source` codes), NEW until the job is seen, the
// ★ saved and ✓ applied markers; the badges and the knockouts; then either the score badge or, for a card
// without a score, its state chip with the preliminary similarity and Score now; and the quick actions.
//
// Every server string is rendered as text. A group card (`kind: "group"`) is its representative's card here
// (Task 25 frames it). The card takes part in the list's roving focus: `tabIndex` 0 when `focused`, else -1;
// Enter on the card itself (not on one of its controls) opens it.
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { useDisplay } from "../app/display";
import { RelativeTime } from "../components/RelativeTime";
import { type TranslationKey, useServerText } from "../i18n/index";
import type { JobCardOut } from "./api";
import { CardBadges } from "./CardBadges";
import { postedLabel, seenToday } from "./format";
import styles from "./JobCard.module.css";
import { KnockoutBadges } from "./KnockoutBadge";
import { QuickActions } from "./QuickActions";
import { ScoreBadge } from "./ScoreBadge";
import { ScoreNowButton } from "./ScoreNowButton";
import { SimilarityBar } from "./SimilarityBar";
import { StateChip } from "./StateChip";

// The target states Score now serves; the server decides the rest (spec §9.6.4).
const SCORE_NOW_STATES: ReadonlySet<string> = new Set(["pending", "budget_deferred", "expired_unscored"]);
const MODE_KEYS: ReadonlyMap<string, TranslationKey> = new Map([
  ["remote", "card.mode.remote"],
  ["hybrid", "card.mode.hybrid"],
  ["onsite", "card.mode.onsite"],
] as const);

export interface JobCardProps {
  card: JobCardOut;
  /** The list's one card in the tab order (roving focus). */
  focused: boolean;
  /** Opens the card's detail (Enter on the card). */
  onOpen: (jobId: number) => void;
}

function Unscored({ card }: { card: JobCardOut }) {
  const similarity = card.target?.similarity ?? null;
  const state = card.target?.state;
  return (
    <div className={styles.unscored}>
      <StateChip jobId={card.job_id} target={card.target} bucket={card.bucket} filterKeyword={card.filter_keyword}
        filterReasons={card.filter_reasons} />
      {similarity !== null && <SimilarityBar value={similarity} />}
      {state !== undefined && SCORE_NOW_STATES.has(state) && <ScoreNowButton jobId={card.job_id} />}
    </div>
  );
}

export function JobCard({ card, focused, onOpen }: JobCardProps) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const { timezone } = useDisplay();
  const headingId = useId();
  const now = new Date();
  const modes = card.work_mode_known
    ? card.work_modes.map((mode) => { const key = MODE_KEYS.get(mode); return key === undefined ? mode : t(key); }).join(" / ")
    : t("card.mode_unknown");
  const where = [...card.locations, modes].filter((part) => part !== "").join(" · ");
  const unseen = card.user === null || card.user.seen_at === null;
  return (
    <article className={styles.card} aria-labelledby={headingId} tabIndex={focused ? 0 : -1}
      onKeyDown={(event) => {
        if (event.key === "Enter" && event.target === event.currentTarget) {
          event.preventDefault();
          onOpen(card.job_id);
        }
      }}>
      <div className={styles.main}>
        <h2 id={headingId} className={styles.title}>{card.title}</h2>
        <p className={styles.company}>{card.company}</p>
        <p className={styles.where}>{where}</p>
        <p className={styles.dates}>
          <span className={styles.date}>
            {t("card.posted")}{" "}
            {card.posted_at === null ? (
              postedLabel(null, card.posted_precision, now, timezone, t)
            ) : (
              <time dateTime={card.posted_at}>
                {postedLabel(card.posted_at, card.posted_precision, now, timezone, t)}
              </time>
            )}
          </span>
          <span className={styles.date}>
            {t("card.first_seen")} <RelativeTime value={card.first_seen_at} tz={timezone} now={now} />
          </span>
          {!seenToday(card.last_seen_at, now, timezone) && (
            <span className={styles.date}>
              {t("card.last_seen")} <RelativeTime value={card.last_seen_at} tz={timezone} now={now} />
            </span>
          )}
        </p>
        <div className={styles.marks}>
          <ul role="list" className={styles.sources} aria-label={t("card.sources")}>
            {card.source_ids.map((source) => (
              <li key={source} className={styles.source}>{serverText("source", source)}</li>
            ))}
          </ul>
          {unseen && <span className={styles.new}>{t("card.new")}</span>}
          {card.user?.saved_at != null && <span className={styles.marker}>{t("card.saved")}</span>}
          {card.user?.applied_at != null && <span className={styles.marker}>{t("card.applied")}</span>}
        </div>
        <CardBadges card={card} />
        <KnockoutBadges knockouts={card.knockouts} />
      </div>
      <div className={styles.side}>
        {card.score !== null ? <ScoreBadge score={card.score} /> : <Unscored card={card} />}
      </div>
      <div className={styles.actions}>
        <QuickActions card={card} />
      </div>
    </article>
  );
}
