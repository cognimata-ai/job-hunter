// A card's quick actions (spec §15.3; plan M4 Task 22, Ruling i): Save, Applied and Hide, toggle buttons whose
// pressed state is the user's (`card.user`), and Open ad ↗ (`links.open_ad`; absent without one).
//
// A toggle writes only its own field (`PUT /api/jobs/{job_id}/actions`), with `apply_to_group: true` on a group
// card, and the list is read again after the answer: nothing changes before it. Hiding raises the notice
// "Hidden" with Undo, which writes `hidden: false` (the card has left the default list by then, so Undo is the
// mutation's, not the card's). A failure is the card's toast (`useCardFailure`): a merged job's says so, and the
// list is read again.
import { useTranslation } from "react-i18next";
import { OutboundLink } from "../components/OutboundLink";
import { useToast } from "../components/Toaster";
import { type CardActionBody, type JobCardOut, useJobActions } from "./api";
import styles from "./QuickActions.module.css";

type Toggle = "saved" | "applied" | "hidden";

export function QuickActions({ card }: { card: JobCardOut }) {
  const { t } = useTranslation();
  const toast = useToast();
  const actions = useJobActions();
  const group: Pick<CardActionBody, "apply_to_group"> = card.kind === "group" ? { apply_to_group: true } : {};
  const on: Record<Toggle, boolean> = {
    saved: card.user?.saved_at != null,
    applied: card.user?.applied_at != null,
    hidden: card.user?.hidden_at != null,
  };
  const toggle = (field: Toggle) => {
    const value = !on[field];
    const body: CardActionBody = { ...group };
    body[field] = value;
    actions.mutate(
      { jobId: card.job_id, body },
      {
        onSuccess: () => {
          if (field === "hidden" && value) {
            toast({
              message: t("card.hidden_toast"),
              action: {
                label: t("action.undo"),
                onAction: () => { actions.mutate({ jobId: card.job_id, body: { hidden: false, ...group } }); },
              },
            });
          }
        },
      },
    );
  };
  const button = (field: Toggle, label: string) => (
    <button type="button" className={styles.toggle} aria-pressed={on[field]} disabled={actions.isPending}
      onClick={() => { toggle(field); }}>
      {label}
    </button>
  );
  return (
    <div role="group" className={styles.actions} aria-label={t("card.actions")}>
      {button("saved", t("card.save"))}
      {button("applied", t("card.mark_applied"))}
      {button("hidden", t("card.hide"))}
      {card.links.open_ad !== null && (
        <OutboundLink href={card.links.open_ad} className={styles.open}>
          {t("card.open_ad")} <span aria-hidden="true">↗</span>
        </OutboundLink>
      )}
    </div>
  );
}
