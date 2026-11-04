// A duplicate group's card (spec §15.5, §7.18; plan M4 Task 25, resolution b): the representative's `JobCard`,
// framed by the group, one card of the list (its roving focus and `j`/`k` are the representative's; the
// disclosure and the members' actions are tab stops inside it).
//
// - The header: "Possible duplicates (<size>) · scored via <sources> / <title> · applies to <size> members", the
//   size counting every member, the representative included (`jobs.cards`).
// - Below the card's actions: the deferred members (the jobs waiting for the representative's score), each with
//   its sources, title, availability, own state and completeness; then Show members, a disclosure. Expanding
//   it reads the representative's detail (`useJobDetail`, the drawer's cache entry) and lists its group's
//   members with their actions (`GroupMembers`). The members stay mounted once shown, so collapsing and
//   expanding again reads nothing.
// - A group that is gone meanwhile (the detail has no group, or the representative is gone) says so and reads
//   the list again, once; one that a write of its own ended says so too, the write having read it again
//   (`GroupMembers`' `onGone`, called before the caches change, so it is known before any render). That write's
//   control went with the members: when focus has dropped, the card's article takes it (focus never stays on
//   `<body>`, Task 23).
// - For a card that is not a group's, it is the plain `JobCard`: the list renders every card through it, so a
//   card that stops (or starts) being a group card keeps its article, and the focus in it.
// - The card's Save, Applied and Hide apply to every member (`QuickActions`' `apply_to_group`).
import { useQueryClient } from "@tanstack/react-query";
import { type RefObject, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useServerText } from "../i18n/index";
import type { JobCardOut } from "./api";
import { goneOf, LISTS_KEY, useJobDetail } from "./detailApi";
import { availabilityText } from "./format";
import styles from "./GroupCard.module.css";
import { GroupMembers, memberStateText, sourceNames } from "./GroupMembers";
import { JobCard, type JobCardProps } from "./JobCard";
import { focusDropped } from "./useListKeyboard";

type GroupCardOut = NonNullable<JobCardOut["group"]>;

function Header({ group }: { group: GroupCardOut }) {
  const { t, i18n } = useTranslation();
  const serverText = useServerText();
  const { size, representative } = group;
  const text = [
    t("group.possible_duplicates", { size: String(size) }),
    t("group.scored_via", {
      sources: sourceNames(representative.source_ids, i18n.language, serverText),
      title: representative.title,
    }),
    t("group.applies", { count: size }),
  ].join(" · ");
  return <p className={styles.header}>{text}</p>;
}

/** The deferred members, in short. */
function Deferred({ group }: { group: GroupCardOut }) {
  const { t, i18n } = useTranslation();
  const serverText = useServerText();
  const labelId = useId();
  if (group.members.length === 0) {
    return null;
  }
  return (
    <div className={styles.deferred}>
      <p id={labelId} className={styles.label}>{t("group.deferred")}</p>
      <ul role="list" aria-labelledby={labelId} className={styles.list}>
        {group.members.map((member) => (
          <li key={member.job_id} className={styles.item}>
            <span className={styles.title}>{member.title}</span>{" "}
            <span className={styles.facts}>
              {[
                sourceNames(member.source_ids, i18n.language, serverText),
                availabilityText(member.availability, t),
                memberStateText(member.target_state, t, serverText),
              ].join(" · ")}
            </span>{" "}
            <span className={styles.completeness}>{serverText("completeness", member.completeness)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

interface MembersProps {
  representativeId: number;
  size: number;
  onOpen: (jobId: number) => void;
  /** The card's article, which takes the focus a write of the members drops. */
  article: RefObject<HTMLElement | null>;
}

/** The group's members, from the representative's detail. */
function Members({ representativeId, size, onOpen, article }: MembersProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const detail = useJobDetail(representativeId);
  const group = detail.data?.group; // `undefined` until read, `null` once the representative has none
  const gone = goneOf(detail.error) !== null || group === null || group?.status === "dissolved";
  const goneSeen = useRef(false);
  const goneByWrite = useRef(false); // a write of these members ended the group, and read the list again
  useEffect(() => {
    if (!gone || goneSeen.current) {
      return;
    }
    goneSeen.current = true;
    if (!goneByWrite.current) {
      void queryClient.invalidateQueries({ queryKey: LISTS_KEY }, { cancelRefetch: false });
    }
  }, [gone, queryClient]);
  useLayoutEffect(() => {
    if (gone && goneByWrite.current && focusDropped()) {
      article.current?.focus();
    }
  }, [gone, article]);

  if (gone) {
    return <p className={styles.gone}>{t("group.gone")}</p>;
  }
  if (group !== undefined) {
    return (
      <GroupMembers group={group} onOpen={onOpen}
        onGone={(jobIds) => {
          if (jobIds.includes(representativeId)) {
            goneByWrite.current = true;
          }
        }} />
    );
  }
  if (detail.isError) {
    return <ProblemBox error={detail.error} onRetry={() => { void detail.refetch(); }} />;
  }
  return <Skeleton variant="line" count={size} />;
}

export function GroupCard(props: JobCardProps) {
  const { card, onOpen } = props;
  const { t } = useTranslation();
  const regionId = useId();
  const [expanded, setExpanded] = useState(false);
  const [shown, setShown] = useState(false); // expanded at least once: the members stay mounted
  const article = useRef<HTMLElement>(null);
  const group = card.group;
  if (group === null) {
    return <JobCard {...props} ref={article} />;
  }
  const footer = (
    <div className={styles.footer}>
      <Deferred group={group} />
      <div>
        <button type="button" className={styles.disclosure} aria-expanded={expanded} aria-controls={regionId}
          onClick={() => {
            setExpanded(!expanded);
            setShown(true);
          }}>
          <span aria-hidden="true" className={styles.chevron}>{expanded ? "▾" : "▸"}</span>{" "}
          {expanded ? t("group.hide_members") : t("group.show_members", { size: String(group.size) })}
        </button>
      </div>
      <div id={regionId} hidden={!expanded} className={styles.members}>
        {shown && (
          <Members representativeId={group.representative.job_id} size={group.size} onOpen={onOpen} article={article} />
        )}
      </div>
    </div>
  );
  return <JobCard {...props} ref={article} header={<Header group={group} />} footer={footer} />;
}
