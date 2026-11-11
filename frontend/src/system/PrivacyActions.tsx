// Privacy & data's actions (spec §15.7, §20.3-§20.5; plan M5 Task 20, Deviations 19, 21, 22): Export my data,
// Purge Adzuna data and Delete everything, each a card.
//
// - **Export my data** sends the keyed `POST /api/privacy/export`, then reads the export every 3 s in the
//   background until it is no longer running: a ready one offers its zip (`?download=1`) with its size and its
//   expiry; a failed, expired or purged one says so. Following the link reads the export again, so a purge that
//   deleted it meanwhile shows.
// - **Purge Adzuna data** and **Delete everything** each open a dialog: what it deletes, §20.4's statement (backup
//   sets age out within about 3 months; the replay of the deletion records keeps the data from returning on a
//   restore; done once it shows "Confirmed offsite"), the phrase to type (`PURGE ADZUNA`, `DELETE EVERYTHING`),
//   the password and, when the session's `totp_enabled`, the TOTP code. Submit is enabled only on the exact
//   phrase, a password and (with TOTP) six digits, and is busy while its request runs: a double submit sends
//   once (`useSingleFlight`). A typed text that is no prefix of the phrase is marked as the mismatch it is. The
//   request's key is the action's (`privacyApi`), kept across the dialog's openings, so a retry of a lost
//   request replays it. On 202 the dialog closes, the card says the deletion was requested, and the deletions
//   (`DeletionStatus`) are read again; delete-everything ends the session when it completes, which leads to the
//   login. A refusal stays in the dialog, shown by its code, never by the server's own text (`PrivacyProblem`,
//   Ruling T20-1 (b)).
import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, type SubmitEvent, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { isProblem, type ProblemError } from "../api/client";
import { useSession } from "../api/session";
import dialog from "../components/ConfirmDialog.module.css";
import { cx } from "../components/cx";
import { ModalLayer } from "../components/ModalLayer";
import { Spinner } from "../components/Spinner";
import fields from "../settings/fields.module.css";
import { useFormat } from "./format";
import styles from "./PrivacyActions.module.css";
import { PrivacyProblem } from "./PrivacyProblem";
import {
  type DeletionKind,
  type DeletionVars,
  downloadHref,
  EXPORT_KEY,
  type ManifestQueuedOut,
  PHRASES,
  useExport,
  useRequestDeletion,
  useSingleFlight,
  useStartExport,
} from "./privacyApi";
import { useUnits } from "./units";

const TOTP_CODE = /^\d{6}$/;

/** The keys of each destructive action's texts. */
const TEXTS = { delete_everything: "delete", source_purge: "purge" } as const satisfies Record<DeletionKind, string>;

function Card({ title, body, danger = false, children }: { title: string; body: string; danger?: boolean; children: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className={cx(styles.card, danger && styles.dangerCard)}>
      <h4 id={id} className={styles.cardTitle}>{title}</h4>
      <p className={styles.cardBody}>{body}</p>
      <div className={styles.cardActions}>{children}</div>
    </section>
  );
}

function ExportAction() {
  const { t } = useTranslation();
  const format = useFormat();
  const units = useUnits();
  const queryClient = useQueryClient();
  const start = useStartExport();
  const send = useSingleFlight(start);
  const [exportId, setExportId] = useState<number | null>(null);
  const [problem, setProblem] = useState<ProblemError | null>(null);
  const state = useExport(exportId);
  const current = state.data;
  // from the request until the export is no longer running (its first read included)
  const running = start.isPending || (exportId !== null && current === undefined && !state.isError) || current?.status === "running";
  return (
    <Card title={t("settings_privacy.export.title")} body={t("settings_privacy.export.body")}>
      <button type="button" className={fields.button} disabled={running} aria-busy={running}
        onClick={() => {
          setProblem(null);
          send(undefined, { onSuccess: (out) => { setExportId(out.export_id); }, onError: setProblem });
        }}>
        {t("settings_privacy.export.start")}
      </button>
      {running && <Spinner label={t("settings_privacy.export.running")} />}
      {current?.status === "ready" && exportId !== null && (
        <div className={styles.ready}>
          <p>
            {t("settings_privacy.export.ready", {
              size: units.bytes(current.size_bytes ?? 0),
              expires: format.dateTimeMinutes(current.expires_at),
            })}
          </p>
          <a href={downloadHref(exportId)} download className={styles.download}
            onClick={() => { void queryClient.invalidateQueries({ queryKey: [...EXPORT_KEY, exportId] }); }}>
            {t("settings_privacy.export.download")}
          </a>
        </div>
      )}
      {current?.status === "failed" && <p className={styles.note}>{t("settings_privacy.export.failed")}</p>}
      {(current?.status === "expired" || current?.status === "purged") && <p className={styles.note}>{t("settings_privacy.export.gone")}</p>}
      {problem !== null && <PrivacyProblem error={problem} keyed />}
      {state.isError && <PrivacyProblem error={state.error} onRetry={() => { void state.refetch(); }} />}
    </Card>
  );
}

interface DeletionDialogProps {
  kind: DeletionKind;
  pending: boolean;
  send: (vars: DeletionVars, options: { onSuccess: (out: ManifestQueuedOut) => void; onError: (error: ProblemError) => void }) => void;
  onClose: () => void;
  onRequested: () => void;
}

function DeletionDialog({ kind, pending, send, onClose, onRequested }: DeletionDialogProps) {
  const { t } = useTranslation();
  const session = useSession();
  const id = useId();
  const totpOn = session.data?.totp_enabled === true;
  const phrase = PHRASES[kind];
  const texts = TEXTS[kind];
  const [confirm, setConfirm] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [problem, setProblem] = useState<ProblemError | null>(null);
  const mismatch = confirm !== "" && !phrase.startsWith(confirm);
  const ready = confirm === phrase && password !== "" && (!totpOn || TOTP_CODE.test(totp));
  const close = () => {
    if (!pending) {
      onClose();
    }
  };
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!ready || pending) {
      return;
    }
    setProblem(null);
    const reauth = { password, totp: totpOn ? totp : null };
    send(
      { kind, confirm, reauth: () => reauth },
      {
        onSuccess: onRequested,
        onError: (error) => {
          setProblem(error);
          if (isProblem(error, "reauth_failed")) {
            setTotp(""); // a code is good once
          }
        },
      },
    );
  };
  const phraseDescribed = [`${id}-phrase-hint`, mismatch ? `${id}-mismatch` : null].filter(Boolean).join(" ");
  return (
    <ModalLayer onDismiss={close} role="alertdialog" labelledBy={`${id}-title`} describedBy={`${id}-what`}
      initialFocus="first" placement="center" className={styles.dialog}>
      <form className={styles.form} noValidate onSubmit={submit}>
        <h2 id={`${id}-title`} className={dialog.title}>{t(`settings_privacy.${texts}.dialog_title`)}</h2>
        <p id={`${id}-what`} className={styles.what}>{t(`settings_privacy.${texts}.what`)}</p>
        <p className={styles.lag}>{t("settings_privacy.later")}</p>
        <div className={fields.field}>
          <label htmlFor={`${id}-phrase`} className={fields.label}>{t("settings_privacy.phrase_label")}</label>
          <p id={`${id}-phrase-hint`} className={fields.hint}>
            {t("settings_privacy.phrase_hint")} <code className={styles.phrase}>{phrase}</code>
          </p>
          <input id={`${id}-phrase`} name="confirm" type="text" className={fields.input} value={confirm}
            autoComplete="off" autoCapitalize="characters" spellCheck={false} aria-invalid={mismatch}
            aria-describedby={phraseDescribed} onChange={(event) => { setConfirm(event.target.value); }} />
          {mismatch && <p id={`${id}-mismatch`} className={styles.mismatch}>{t("problem.confirmation_mismatch")}</p>}
        </div>
        <div className={fields.field}>
          <label htmlFor={`${id}-password`} className={fields.label}>{t("login.password")}</label>
          <input id={`${id}-password`} name="password" type="password" className={fields.input} value={password}
            autoComplete="current-password" aria-describedby={`${id}-reauth`}
            onChange={(event) => { setPassword(event.target.value); }} />
        </div>
        {totpOn && (
          <div className={fields.field}>
            <label htmlFor={`${id}-totp`} className={fields.label}>{t("login.totp")}</label>
            <input id={`${id}-totp`} name="totp" type="text" inputMode="numeric" autoComplete="one-time-code" maxLength={6}
              className={cx(fields.input, styles.code)} value={totp} aria-describedby={`${id}-totp-hint ${id}-reauth`}
              onChange={(event) => { setTotp(event.target.value.trim()); }} />
            <p id={`${id}-totp-hint`} className={fields.hint}>{t("login.totp_hint")}</p>
          </div>
        )}
        <p id={`${id}-reauth`} className={fields.hint}>{t("settings_privacy.reauth_hint")}</p>
        {problem !== null && <PrivacyProblem error={problem} keyed />}
        <div className={dialog.actions}>
          <button type="button" className={dialog.secondary} disabled={pending} onClick={close}>{t("action.cancel")}</button>
          <button type="submit" className={styles.danger} disabled={!ready || pending} aria-busy={pending}>
            {pending && <span className={dialog.busy} aria-hidden="true" />}
            {t(`settings_privacy.${texts}.submit`)}
          </button>
        </div>
      </form>
    </ModalLayer>
  );
}

function DeletionAction({ kind }: { kind: DeletionKind }) {
  const { t } = useTranslation();
  const request = useRequestDeletion();
  const send = useSingleFlight(request);
  const [open, setOpen] = useState(false);
  const [requested, setRequested] = useState(false);
  const texts = TEXTS[kind];
  return (
    <Card title={t(`settings_privacy.${texts}.title`)} body={t(`settings_privacy.${texts}.body`)} danger={kind === "delete_everything"}>
      <button type="button" className={styles.dangerOutline} onClick={() => { setOpen(true); }}>
        {t(`settings_privacy.${texts}.open`)}
      </button>
      {requested && <p role="status" className={styles.requested}>{t("settings_privacy.requested")}</p>}
      {open && (
        <DeletionDialog kind={kind} pending={request.isPending} send={send}
          onClose={() => { setOpen(false); }}
          onRequested={() => {
            setOpen(false);
            setRequested(true);
          }} />
      )}
    </Card>
  );
}

export function PrivacyActions() {
  const { t } = useTranslation();
  const id = useId();
  return (
    <section aria-labelledby={id} className={styles.actions}>
      <h3 id={id} className={styles.heading}>{t("settings_privacy.actions")}</h3>
      <div className={styles.cards}>
        <ExportAction />
        <DeletionAction kind="source_purge" />
        <DeletionAction kind="delete_everything" />
      </div>
    </section>
  );
}
