// The CV upload and paste (spec §12.1, §12.7, §15.7; plan M4 Task 27, resolution e).
//
// - The drop zone wraps a real file input with a visible label, so the keyboard reaches it; a drop of anything
//   but one file is ignored, with a text. A picked file is sent with [Upload CV].
// - "Send original PDF to Claude" is enabled for a PDF only. Its disclosure is the checkbox's description
//   (`aria-describedby`), always in the accessibility tree; ⓘ only shows it or hides it from view.
// - [Paste text instead] discloses a text area. Its length is counted as the server counts it (code points, NUL
//   characters dropped) and shown against the 50,000 the server takes; under 300 or over 50,000 it cannot be sent
//   (Ruling T27-g), nor over the body's 1 MiB, a silent guard. The paste repair opens it with the focus in it.
// - With a draft in review (`text_review`, `ready`) an upload or a paste asks first: the server discards that
//   draft (D18). While one is being processed both controls are disabled.
// - Neither request is keyed (Ruling T27-c). A refused file or text shows each `errors[].code` through the
//   `upload_reason` namespace; a refused opt-in shows at the checkbox; a 409 says a CV is being processed.
import { type DragEvent, type SyntheticEvent, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { isProblem, type ProblemError } from "../../api/client";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { cx } from "../../components/cx";
import { ProblemBox } from "../../components/ProblemBox";
import { useServerText } from "../../i18n/index";
import { useFormat } from "../../system/format";
import {
  type DraftStatusCode,
  isInReview,
  isProcessing,
  PASTE_MAX,
  PASTE_MAX_BYTES,
  PASTE_MIN,
  pasteLength,
  usePasteCv,
  useUploadCv,
} from "./api";
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
  const [asking, setAsking] = useState<"upload" | "paste" | null>(null);
  const inputId = useId();
  const nativeId = useId();
  const disclosureId = useId();
  const nativeErrorId = useId();
  const pasteId = useId();
  const sizeId = useId();
  const limitId = useId();
  const pasteArea = useRef<HTMLTextAreaElement>(null);
  const processing = isProcessing(openStatus ?? undefined);
  const busy = processing || upload.isPending || paste.isPending;
  const pdf = isPdf(file);
  const chars = pasteLength(text);
  const tooShort = chars < PASTE_MIN;
  const tooLong = chars > PASTE_MAX;
  const pastable = !tooShort && !tooLong && new TextEncoder().encode(text).length <= PASTE_MAX_BYTES;
  const nativeError = upload.error?.errors.find((e) => e.field === NATIVE_FIELD);
  const serverText = useServerText();
  useEffect(() => {
    if (pasteOpen) {
      pasteArea.current?.focus();
    }
  }, [pasteOpen]);

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
    if (pastable && !busy) {
      askOr("paste");
    }
  };
  const max = format.number(PASTE_MAX);
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
          {nativeError !== undefined && (
            <p id={nativeErrorId} className={controls.error}>
              {serverText("draft_failure", nativeError.code, nativeError.message)}
            </p>
          )}
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
          <textarea ref={pasteArea} id={pasteId} className={controls.textarea} rows={10} value={text} disabled={busy}
            aria-invalid={tooLong} aria-describedby={tooShort || tooLong ? `${sizeId} ${limitId}` : sizeId}
            onChange={(event) => { setText(event.target.value); }} />
          <div className={styles.meter}>
            {(tooShort || tooLong) && (
              <p id={limitId} className={tooLong ? controls.error : styles.hint}>
                {tooLong ? t("cv.paste.too_long", { max }) : t("cv.paste.too_short", { min: format.number(PASTE_MIN) })}
              </p>
            )}
            <p id={sizeId} className={cx(styles.size, tooLong && styles.over)}>
              {t("cv.paste.count", { count: chars, n: format.number(chars), max })}
            </p>
          </div>
          {paste.error !== null && <Refusal error={paste.error} />}
          <button type="submit" className={controls.primary} disabled={busy || !pastable} aria-busy={paste.isPending}>
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
