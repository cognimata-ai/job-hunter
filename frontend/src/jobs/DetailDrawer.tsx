// The detail drawer (spec §15.4; plan M4 Task 24, resolution d): a job's detail in the modal `Drawer`.
//
// - The header: the title and company; the places, the work modes ("(structured: <source>)" when a structured
//   field gave them), the employment types and the salary (min–max or one amount, its period, "(stated)" or
//   "(Adzuna estimate)"); posted with its precision, first seen, last seen unless today, and the availability;
//   the Seen, Save, Applied and Hide toggles, Open ad ↗ (the one outbound link, Ruling T24-a) and, for an Adzuna
//   ad, the Adzuna badge.
// - The score: the badge (M and the interval bar), "L · U · coverage · confidence", the status and the critic's
//   outcome, "scored via <representative>" for a deferred member (B26), the versions line (the model ids are data,
//   shown as served), the summary in the UI language, the criteria and "Knockouts · Caps · Industry". Without a
//   score, the state chip's text (Task 22's).
// - The tabs: Description, Contested, Provenance, History and Feedback, the same for every job, back at
//   Description for each new job.
//
// Opening a job that has never been seen sends `seen_on_open` once per open of that job (a ref, so StrictMode's
// second effect sends nothing). A 404 is no problem box (B28): a merged job shows its survivor instead, with a
// notice, and a job that no longer exists says so with a way back.
import * as Tabs from "@radix-ui/react-tabs";
import type { TFunction } from "i18next";
import { type ReactNode, useEffect, useId, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useDisplay } from "../app/display";
import { Drawer } from "../components/Drawer";
import { EmptyState } from "../components/EmptyState";
import { OutboundLink } from "../components/OutboundLink";
import { ProblemBox } from "../components/ProblemBox";
import { RelativeTime } from "../components/RelativeTime";
import { Skeleton } from "../components/Skeleton";
import { useToast } from "../components/Toaster";
import { intlLocale, type TranslationKey, useServerText } from "../i18n/index";
import { type CardActionBody, useJobActions } from "./api";
import { ContestedTab } from "./ContestedTab";
import { CriteriaTable } from "./CriteriaTable";
import { DescriptionTab } from "./DescriptionTab";
import styles from "./DetailDrawer.module.css";
import { type DetailScoreOut, goneOf, type JobDetailOut, type JobOut, useJobDetail, useSeenOnOpen } from "./detailApi";
import { FeedbackTab } from "./FeedbackTab";
import { availabilityText, percentInt, postedLabel, scoreInt, seenToday, type ServerText } from "./format";
import { HistoryTab } from "./HistoryTab";
import { JobRef } from "./JobRef";
import { KnockoutBadges, shownKnockouts } from "./KnockoutBadge";
import { ProvenanceTab } from "./ProvenanceTab";
import { ScoreBadge } from "./ScoreBadge";
import { chipText } from "./StateChip";

export interface DetailDrawerProps {
  jobId: number;
  onClose: () => void;
  /** Shows another job in the drawer (a survivor, a representative). */
  onNavigate: (jobId: number) => void;
}

const MODE_KEYS: ReadonlyMap<string, TranslationKey> = new Map([
  ["remote", "card.mode.remote"],
  ["hybrid", "card.mode.hybrid"],
  ["onsite", "card.mode.onsite"],
] as const);
// The precisions a posted label already states ("date unknown", "(ingest date)").
const PRECISION_IN_LABEL: ReadonlySet<string> = new Set(["unknown", "ingest"]);
const TABS = ["description", "contested", "provenance", "history", "feedback"] as const;
const TAB_LABELS = {
  description: "drawer.tab.description",
  contested: "drawer.tab.contested",
  provenance: "drawer.tab.provenance",
  history: "drawer.tab.history",
  feedback: "drawer.tab.feedback",
} as const satisfies Record<(typeof TABS)[number], TranslationKey>;
const PROMPT_HASH_SHOWN = 8;

type Toggle = "seen" | "saved" | "applied" | "hidden";

/** A salary's amounts in its currency (an unknown currency code: the amount and the code). */
function amountText(amount: string, currency: string, locale: string): string {
  try {
    return new Intl.NumberFormat(locale, { style: "currency", currency, trailingZeroDisplay: "stripIfInteger" }).format(
      amount as Intl.StringNumericLiteral,
    );
  } catch {
    return `${amount} ${currency}`; // not an ISO 4217 code
  }
}

/** "€70,000–€85,000 per year (stated)", or one amount when only one is known; `null` without an amount. */
function salaryText(salary: JobOut["salary"], locale: string, t: TFunction, serverText: ServerText): string | null {
  if (salary === null) {
    return null;
  }
  const amounts = [salary.min, salary.max].filter((amount): amount is string => amount !== null);
  const shown = [...new Set(amounts)].map((amount) => amountText(amount, salary.currency, locale));
  if (shown.length === 0) {
    return null;
  }
  return t("drawer.salary", {
    amount: shown.join("–"),
    period: serverText("salary_period", salary.period),
    kind: t(salary.kind === "stated" ? "drawer.salary_stated" : "drawer.salary_estimate"),
  });
}

function Facts({ job }: { job: JobOut }) {
  const { t, i18n } = useTranslation();
  const serverText = useServerText();
  const { timezone } = useDisplay();
  const locale = intlLocale(i18n.language);
  const now = new Date();
  let modes = job.work_mode_known
    ? job.work_modes.map((mode) => { const key = MODE_KEYS.get(mode); return key === undefined ? mode : t(key); }).join(" / ")
    : t("card.mode_unknown");
  if (job.work_mode_known && job.work_mode_source_id !== null) {
    modes = `${modes} ${t("drawer.structured", { source: serverText("source", job.work_mode_source_id) })}`;
  }
  const employment =
    job.employment_types.length === 0
      ? null
      : new Intl.ListFormat(locale, { type: "conjunction" }).format(job.employment_types.map((type) => serverText("employment", type)));
  const where = [...job.locations, modes, employment, salaryText(job.salary, locale, t, serverText)]
    .filter((part): part is string => part !== null && part !== "")
    .join(" · ");
  const posted = postedLabel(job.posted_at, job.posted_precision, now, timezone, t);
  return (
    <>
      <p className={styles.fact}>{where}</p>
      <p className={styles.fact}>
        <span>
          {t("card.posted")} {job.posted_at === null ? posted : <time dateTime={job.posted_at}>{posted}</time>}
          {!PRECISION_IN_LABEL.has(job.posted_precision) &&
            ` ${t("drawer.precision", { precision: serverText("posted_precision", job.posted_precision) })}`}
        </span>
        {" · "}
        <span>
          {t("card.first_seen")} <RelativeTime value={job.first_seen_at} tz={timezone} now={now} />
        </span>
        {!seenToday(job.last_seen_at, now, timezone) && (
          <>
            {" · "}
            <span>
              {t("card.last_seen")} <RelativeTime value={job.last_seen_at} tz={timezone} now={now} />
            </span>
          </>
        )}
        {" · "}
        <span>{availabilityText(job.availability, t)}</span>
      </p>
    </>
  );
}

function Actions({ detail }: { detail: JobDetailOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const actions = useJobActions();
  const { job, user, links } = detail;
  const on: Record<Toggle, boolean> = {
    seen: user?.seen_at != null,
    saved: user?.saved_at != null,
    applied: user?.applied_at != null,
    hidden: user?.hidden_at != null,
  };
  const button = (field: Toggle, label: string) => (
    <button type="button" className={styles.toggle} aria-pressed={on[field]} disabled={actions.isPending}
      onClick={() => {
        const body: CardActionBody = {};
        body[field] = !on[field];
        actions.mutate({ jobId: job.job_id, body });
      }}>
      {label}
    </button>
  );
  const adzuna = serverText("badge", "adzuna");
  return (
    <div role="group" className={styles.actions} aria-label={t("card.actions")}>
      {button("seen", t("drawer.seen"))}
      {button("saved", t("card.save"))}
      {button("applied", t("card.mark_applied"))}
      {button("hidden", t("card.hide"))}
      {links.open_ad !== null && (
        <OutboundLink href={links.open_ad} className={styles.open}>
          {t("card.open_ad")} <span aria-hidden="true">↗</span>
        </OutboundLink>
      )}
      {job.source_ids.includes("adzuna") &&
        (links.adzuna === null ? (
          <span className={styles.attribution}>{adzuna}</span>
        ) : (
          <OutboundLink href={links.adzuna} className={styles.attribution}>{adzuna}</OutboundLink>
        ))}
    </div>
  );
}

function Scored({ detail, score, onNavigate }: { detail: JobDetailOut; score: DetailScoreOut; onNavigate: (jobId: number) => void }) {
  const { t, i18n } = useTranslation();
  const serverText = useServerText();
  const low = scoreInt(score.low);
  const high = Math.max(low, scoreInt(score.high));
  const { versions } = score;
  const generator = `${versions.generator.model}/${versions.generator.effort}`;
  const critic = versions.critic === null ? "" : ` → ${t("drawer.critic_model", { model: `${versions.critic.model}/${versions.critic.effort}` })}`;
  const lang = i18n.language === "de" ? "de" : "en";
  const summaryPath = `summary_${lang}`;
  const knockouts = shownKnockouts(score.knockouts);
  const caps = score.caps_applied.map((cap) => `${serverText("cap", cap.code)} (${serverText("cap_kind", cap.kind)})`);
  return (
    <>
      <div className={styles.scoreLine}>
        <ScoreBadge score={score} />
        <div className={styles.scoreFacts}>
          <p>
            {t("drawer.score_facts", {
              low: String(low),
              high: String(high),
              percent: String(percentInt(score.coverage)),
              confidence: serverText("confidence", score.confidence),
            })}
          </p>
          <p>
            {t("drawer.status", {
              status: serverText("result_status", score.result_status),
              critic: serverText("critic_outcome", score.critic_outcome),
            })}
          </p>
          {score.scored_via_job_id !== null && (
            <p>
              {t("drawer.scored_via")} <JobRef jobId={score.scored_via_job_id} detail={detail} onNavigate={onNavigate} />
            </p>
          )}
        </div>
      </div>
      <p className={styles.versions}>
        {t("drawer.versions", {
          profile: String(versions.profile_version_no),
          rubric: versions.rubric_version,
          prompt: versions.prompt_schema_hash.slice(0, PROMPT_HASH_SHOWN),
          generator,
        })}
        {critic}
      </p>
      <div className={styles.summary}>
        <p className={styles.label}>
          {t("drawer.summary")}
          {score.corrected_fields.includes(summaryPath) && <> <span className={styles.marker}>{t("drawer.corrected")}</span></>}
          {score.shortened_fields.includes(summaryPath) && <> <span className={styles.marker}>{t("badge.shortened")}</span></>}
        </p>
        <p lang={lang}>{lang === "de" ? score.summary_de : score.summary_en}</p>
      </div>
      <CriteriaTable score={score} />
      <div className={styles.limits}>
        <div className={styles.limit}>
          {t("card.knockouts")}: {knockouts.length === 0 ? t("drawer.none") : <KnockoutBadges knockouts={knockouts} />}
        </div>
        <div className={styles.limit}>
          {t("drawer.caps")}: {caps.length === 0 ? t("drawer.none") : caps.join(", ")}
        </div>
        <div className={styles.limit}>
          {t("drawer.industry")}: {serverText("industry_match", score.industry_match)}
        </div>
      </div>
    </>
  );
}

function ScoreSection({ detail, onNavigate }: { detail: JobDetailOut; onNavigate: (jobId: number) => void }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const headingId = useId();
  const { score, target } = detail;
  // a published target without a score is a verified not-a-job-ad (the card's `not_a_job_ad` bucket)
  const bucket = target?.state.startsWith("published_") === true ? "not_a_job_ad" : "unscored";
  return (
    <section aria-labelledby={headingId} className={styles.score}>
      <h3 id={headingId} className={styles.heading}>{t("drawer.score")}</h3>
      {score === null ? (
        <p className={styles.chip}>{chipText(target, bucket, null, [], t, serverText)}</p>
      ) : (
        <Scored detail={detail} score={score} onNavigate={onNavigate} />
      )}
    </section>
  );
}

function DetailTabs({ detail, onNavigate }: { detail: JobDetailOut; onNavigate: (jobId: number) => void }) {
  const { t } = useTranslation();
  const jobId = detail.job.job_id;
  const panels: Record<(typeof TABS)[number], ReactNode> = {
    description: <DescriptionTab detail={detail} onNavigate={onNavigate} />,
    contested: <ContestedTab contested={detail.contested} />,
    provenance: <ProvenanceTab detail={detail} />,
    history: <HistoryTab jobId={jobId} first={detail.history} />,
    feedback: <FeedbackTab jobId={jobId} feedback={detail.feedback} />,
  };
  return (
    <Tabs.Root defaultValue="description" className={styles.tabs}>
      <Tabs.List aria-label={t("drawer.tabs")} className={styles.tabList}>
        {TABS.map((tab) => (
          <Tabs.Trigger key={tab} value={tab} className={styles.tab}>{t(TAB_LABELS[tab])}</Tabs.Trigger>
        ))}
      </Tabs.List>
      {TABS.map((tab) => (
        <Tabs.Content key={tab} value={tab} className={styles.panel}>{panels[tab]}</Tabs.Content>
      ))}
    </Tabs.Root>
  );
}

function Detail({ detail, titleId, onNavigate }: { detail: JobDetailOut; titleId: string; onNavigate: (jobId: number) => void }) {
  return (
    <div className={styles.detail}>
      <header className={styles.header}>
        <h2 id={titleId} className={styles.title}>{detail.job.title}</h2>
        <p className={styles.company}>{detail.job.company}</p>
        <Facts job={detail.job} />
        <Actions detail={detail} />
      </header>
      <ScoreSection detail={detail} onNavigate={onNavigate} />
      <DetailTabs detail={detail} onNavigate={onNavigate} />
    </div>
  );
}

export function DetailDrawer({ jobId, onClose, onNavigate }: DetailDrawerProps) {
  const { t } = useTranslation();
  const toast = useToast();
  const titleId = useId();
  const detail = useJobDetail(jobId);
  const gone = goneOf(detail.error);
  const survivor = gone?.survivor;

  // a merged job: its survivor instead, once (StrictMode runs an effect twice)
  const redirected = useRef<number | null>(null);
  useEffect(() => {
    if (survivor !== undefined && survivor !== jobId && redirected.current !== jobId) {
      redirected.current = jobId;
      toast({ message: t("drawer.merged") });
      onNavigate(survivor);
    }
  }, [survivor, jobId, onNavigate, toast, t]);

  // never seen: `seen_on_open`, once per job for this open of the drawer
  const { mutate: markSeen } = useSeenOnOpen();
  const sent = useRef(new Set<number>());
  const data = detail.data;
  const unseen = data !== undefined && (data.user === null || data.user.seen_at === null);
  useEffect(() => {
    if (unseen && !sent.current.has(jobId)) {
      sent.current.add(jobId);
      markSeen(jobId);
    }
  }, [unseen, jobId, markSeen]);

  const untitled = <h2 id={titleId} className="visually-hidden">{t("drawer.title")}</h2>;
  let content: ReactNode;
  if (gone !== null && survivor === undefined) {
    content = (
      <>
        {untitled}
        <EmptyState title={t("drawer.gone")}
          action={<button type="button" className={styles.toggle} onClick={onClose}>{t("drawer.close")}</button>} />
      </>
    );
  } else if (gone === null && data !== undefined) {
    content = <Detail key={jobId} detail={data} titleId={titleId} onNavigate={onNavigate} />;
  } else if (gone === null && detail.isError) {
    content = (
      <>
        {untitled}
        <ProblemBox error={detail.error} onRetry={() => { void detail.refetch(); }} />
      </>
    );
  } else {
    content = (
      <>
        {untitled}
        <Skeleton variant="drawer" />
      </>
    );
  }
  return (
    <Drawer open onClose={onClose} titleId={titleId}>
      {content}
    </Drawer>
  );
}
