// The badges of a card (spec §15.3, §15.5, §7.18; plan M4 Task 22, Ruling g, B17, B27): `card.badges` in the
// server's order (`jobs.cards.Badge`), each named by its `badge.<code>` text, then "previous: 72 (profile v3)"
// while it is re-scored, "possible duplicate of <job>" for a group member with its own score, and "Not
// scorable: <reason>" for a member of a group without a representative. Every badge is text; its colour only
// repeats it. The Adzuna attribution badge links to the ad's Adzuna page (`links.adzuna`, at least 116×23 px,
// spec §5) when it has one, and is plain text otherwise.
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { OutboundLink } from "../components/OutboundLink";
import { useServerText } from "../i18n/index";
import type { JobCardOut } from "./api";
import styles from "./CardBadges.module.css";
import { scoreInt } from "./format";

type Badge = JobCardOut["badges"][number];
type Tone = "neutral" | "info" | "warn" | "critical" | "attribution";

const TONE: Record<Badge, Tone> = {
  contested: "warn",
  partial: "neutral",
  shortened: "neutral",
  possibly_not_a_job_ad: "warn",
  injection_suspected: "critical",
  stale: "warn",
  closed: "critical",
  adzuna: "attribution",
};

interface Item {
  key: string;
  tone: Tone;
  content: ReactNode;
}

export function CardBadges({ card }: { card: JobCardOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const items: Item[] = card.badges.map((badge) => {
    const text = serverText("badge", badge);
    const href = badge === "adzuna" ? card.links.adzuna : null;
    return {
      key: badge,
      tone: TONE[badge],
      content: href === null ? text : <OutboundLink href={href} className={styles.link}>{text}</OutboundLink>,
    };
  });
  if (card.previous !== null) {
    const { mid, profile_version_no: version } = card.previous;
    items.push({
      key: "previous",
      tone: "info",
      content: t("card.previous", { score: String(scoreInt(mid)), version: String(version) }),
    });
  }
  if (card.possible_duplicate_of !== null) {
    items.push({
      key: "possible_duplicate",
      tone: "info",
      content: t("card.possible_duplicate", { title: card.possible_duplicate_of.title }),
    });
  }
  if (card.group_badge !== null) {
    const reason = card.group_badge.unscorable_reason;
    items.push({
      key: "not_scorable",
      tone: "critical",
      content:
        reason === null
          ? t("card.not_scorable_plain")
          : t("card.not_scorable", { reason: serverText("group_unscorable", reason) }),
    });
  }
  if (items.length === 0) {
    return null;
  }
  return (
    <ul role="list" className={styles.badges} aria-label={t("card.badges")}>
      {items.map((item) => (
        <li key={item.key} className={cx(styles.badge, styles[item.tone])}>{item.content}</li>
      ))}
    </ul>
  );
}
