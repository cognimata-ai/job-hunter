// The CV upload and paste (spec §12.1, §12.7, §15.7; plan M4 Task 27, resolution e).
//
// - The drop zone wraps a real file input with a visible label, so the keyboard reaches it; a drop of anything
//   but one file is ignored, with a text. A picked file is sent with [Upload CV].
// - "Send original PDF to Claude" is enabled for a PDF only. Its disclosure is the checkbox's description
//   (`aria-describedby`), always in the accessibility tree; ⓘ only shows it or hides it from view.
// - [Paste text instead] discloses a text area; its size is shown against the 1 MiB cap in UTF-8 bytes, and an
//   empty or over-cap text is refused before anything is sent.
// - With a draft in review (`text_review`, `ready`) an upload or a paste asks first: the server discards that
//   draft (D18). While one is being processed both controls are disabled.
// - Neither request is keyed (Ruling T27-c). A refused file or text shows each `errors[].code` through the
//   `upload_reason` namespace; a refused opt-in shows at the checkbox; a 409 says a CV is being processed.
import { type DragEvent, type SyntheticEvent, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { isProblem, type ProblemError } from "../../api/client";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { cx } from "../../components/cx";
import { ProblemBox } from "../../components/ProblemBox";
import { useServerText } from "../../i18n/index";
import { useFormat } from "../../system/format";
import { type DraftStatusCode, isInReview, isProcessing, PASTE_MAX_BYTES, usePasteCv, useUploadCv } from "./api";
import controls from "./controls.module.css";
import styles from "./CvUpload.module.css";

const ACCEPT =
  ".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const NATIVE_FIELD = "native_pdf_opt_in";

export interface CvUploadProps {
  /** The open draft's status; `null` without one. */
  openStatus: DraftStatusCode | null;
  /** Open the paste form at once (a failed draft's paste repair). */
  pasteOpen?: boolean;
  /** A new draft was opened. */
  onCreated: (draftId: number) => void;
}

function isPdf(file: File | null): boolean {
  return file !== null && (file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf"));
}

/** A refused upload or paste: each reason through `upload_reason`, else the problem itself. */
function Refusal({ error }: { error: ProblemError }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  if (error.code === "upload_rejected" && error.errors.length > 0) {
    return (
      <div role="alert" className={controls.error}>
        <ul role="list" className={styles.reasons}>
          {error.errors.map((e, i) => <li key={i}>{serverText("upload_reason", e.code, e.message)}</li>)}
        </ul>
      </div>
    );
  }
  if (isProblem(error, "conflict_state")) {
    return <p role="alert" className={controls.error}>{t("cv.upload.conflict")}</p>;
  }
  return <ProblemBox error={error} />;
}

export function CvUpload({ openStatus, pasteOpen = false, onCreated }: CvUploadProps) {
  const { t } = useTranslation();
  const format = useFormat();
  const upload = useUploadCv();
  const paste = usePasteCv();
  const [file, setFile] = useState<File | null>(null);
  const [native, setNative] = useState(false);
  const [dropRefused, setDropRefused] = useState(false);
  const [infoShown, setInfoShown] = useState(false);
  const [pasting, setPasting] = useState(pasteOpen);
  const [text, setText] = useState("");
  const [pasteRefusal, setPasteRefusal] = useState<"empty" | "too_large" | null>(null);
  const [asking, setAsking] = useState<"upload" | "paste" | null>(null);
  const inputId = useId();
  const nativeId = useId();
  const disclosureId = useId();
  const nativeErrorId = useId();
  const pasteId = useId();
  const sizeId = useId();
  const pasteErrorId = useId();
  const processing = isProcessing(openStatus ?? undefined);
  const busy = processing || upload.isPending || paste.isPending;
  const pdf = isPdf(file);
  const bytes = new TextEncoder().encode(text).length;
  const overCap = bytes > PASTE_MAX_BYTES;
  const nativeError = upload.error?.errors.find((e) => e.field === NATIVE_FIELD);

  const pick = (picked: File | null) => {
    setFile(picked);
    setDropRefused(false);
    if (!isPdf(picked)) {
      setNative(false);
    }
    upload.reset();
  };
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (busy) {
      return;
    }
    const files = event.dataTransfer.files;
    if (files.length !== 1) {
      setDropRefused(true);
      return;
    }
    pick(files[0]);
  };
  const sendUpload = () => {
    if (file !== null) {
      upload.mutate({ file, nativePdf: native && pdf }, { onSuccess: ({ draft_id }) => { onCreated(draft_id); } });
    }
  };
  const sendPaste = () => {
    paste.mutate(text, { onSuccess: ({ draft_id }) => { onCreated(draft_id); } });
  };
  const askOr = (action: "upload" | "paste") => {
    if (isInReview(openStatus ?? undefined)) {
      setAsking(action);
    } else if (action === "upload") {
      sendUpload();
    } else {
      sendPaste();
    }
  };
  const submitUpload = (event: SyntheticEvent) => {
    event.preventDefault();
    if (file !== null && !busy) {
      askOr("upload");
    }
  };
  const submitPaste = (event: SyntheticEvent) => {
    event.preventDefault();
    if (busy) {
      return;
    }
    if (text.trim() === "") {
      setPasteRefusal("empty");
    } else if (overCap) {
      setPasteRefusal("too_large");
    } else {
      setPasteRefusal(null);
      askOr("paste");
    }
  };
  const max = format.number(PASTE_MAX_BYTES);
  return (
    <div className={styles.upload}>
      {processing && <p className={styles.processing}>{t("cv.upload.processing")}</p>}
      <form className={styles.row} onSubmit={submitUpload} noValidate aria-label={t("cv.upload.form")}>
        <div className={cx(styles.drop, busy && styles.dropDisabled)} data-testid="cv-drop"
          onDragOver={(event) => { event.preventDefault(); }} onDrop={onDrop}>
          <label htmlFor={inputId} className={styles.dropLabel}>{t("cv.upload.browse")}</label>
          <input id={inputId} type="file" accept={ACCEPT} className={styles.file} disabled={busy}
            onChange={(event) => { pick(event.target.files?.[0] ?? null); }} />
          {file !== null && <p className={styles.picked}>{file.name}</p>}
          {dropRefused && <p className={controls.error}>{t("cv.upload.drop_one")}</p>}
          <button type="submit" className={cx(controls.primary, styles.submit)} disabled={file === null || busy}
            aria-busy={upload.isPending}>
            {t("cv.upload.submit")}
          </button>
        </div>
        <div className={styles.native}>
          <div className={controls.check}>
            <input id={nativeId} type="checkbox" checked={native && pdf} disabled={!pdf || busy}
              aria-invalid={nativeError !== undefined}
              aria-describedby={nativeError === undefined ? disclosureId : `${disclosureId} ${nativeErrorId}`}
              onChange={(event) => { setNative(event.target.checked); }} />
            <label htmlFor={nativeId}>{t("cv.upload.native")}</label>
            <button type="button" className={styles.info} aria-expanded={infoShown} aria-controls={disclosureId}
              aria-label={t("cv.upload.native_info")} onClick={() => { setInfoShown(!infoShown); }}>
              <span aria-hidden="true">ⓘ</span>
            </button>
          </div>
          <p id={disclosureId} className={infoShown ? styles.disclosure : "visually-hidden"}>{t("cv.native.disclosure")}</p>
          {nativeError !== undefined && <p id={nativeErrorId} className={controls.error}>{nativeError.message}</p>}
        </div>
      </form>
      {upload.error !== null && nativeError === undefined && <Refusal error={upload.error} />}
      <button type="button" className={controls.secondary} aria-expanded={pasting} disabled={busy}
        onClick={() => { setPasting(!pasting); }}>
        {t("cv.paste.open")}
      </button>
      {pasting && (
        <form className={styles.paste} onSubmit={submitPaste} noValidate aria-label={t("cv.paste.form")}>
          <label htmlFor={pasteId} className={controls.label}>{t("cv.paste.label")}</label>
          <textarea id={pasteId} className={controls.textarea} rows={10} value={text} disabled={busy}
            aria-invalid={overCap || pasteRefusal !== null}
            aria-describedby={pasteRefusal === null ? sizeId : `${sizeId} ${pasteErrorId}`}
            onChange={(event) => { setText(event.target.value); setPasteRefusal(null); }} />
          <p id={sizeId} className={cx(styles.size, overCap && styles.over)}>
            {t("cv.paste.size", { size: format.number(bytes), max })}
          </p>
          {pasteRefusal !== null && (
            <p id={pasteErrorId} role="alert" className={controls.error}>
              {pasteRefusal === "empty" ? t("cv.paste.empty") : t("cv.paste.too_large", { max })}
            </p>
          )}
          {paste.error !== null && <Refusal error={paste.error} />}
          <button type="submit" className={controls.primary} disabled={busy} aria-busy={paste.isPending}>
            {t("cv.paste.submit")}
          </button>
        </form>
      )}
      <ConfirmDialog open={asking !== null} title={t("cv.replace.title")} confirmLabel={t("cv.replace.confirm")}
        onCancel={() => { setAsking(null); }}
        onConfirm={() => {
          const action = asking;
          setAsking(null);
          if (action === "upload") {
            sendUpload();
          } else if (action === "paste") {
            sendPaste();
          }
        }}>
        <p>{t("cv.replace.body")}</p>
      </ConfirmDialog>
    </div>
  );
}
