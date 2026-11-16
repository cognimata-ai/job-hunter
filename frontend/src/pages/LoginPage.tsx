// Login (spec §15.2): Username, Password and Sign in; the TOTP code field appears once the server answers
// `totp_required`, and the form is sent again with it. Every failure of the credentials is the one generic
// "Invalid credentials"; a lockout or a full login gate is `throttled` with `retry_after_s`. On success the
// user goes back where the login was asked for, a same-origin path only (`safeRedirect`).
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { type SubmitEvent, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { apiFetch, isProblem, type ProblemError } from "../api/client";
import { safeRedirect, SESSION_KEY } from "../api/session";
import { throttleWait } from "../api/throttle";
import { ProblemBox } from "../components/ProblemBox";
import { browserLanguage } from "../i18n/index";
import styles from "./LoginPage.module.css";

const TOTP_CODE = /^\d{6}$/;

interface LoginIn {
  username: string;
  password: string;
  totp?: string;
}

type Message = { kind: "error" | "info"; key: MessageKey; minutes?: number };
type MessageKey =
  | "login.invalid"
  | "login.missing"
  | "login.totp_required"
  | "login.totp_format"
  | "login.throttled"
  | "login.busy"
  | "problem.throttled";

/** A busy gate or a lockout, as `throttleWait` reads its wait. */
function throttledMessage(error: ProblemError): Message {
  const wait = throttleWait(error);
  switch (wait.kind) {
    case "busy":
      return { kind: "error", key: "login.busy" };
    case "minutes":
      return { kind: "error", key: "login.throttled", minutes: wait.minutes };
    case "unknown":
      return { kind: "error", key: "problem.throttled" };
  }
}

export function LoginPage() {
  const { t, i18n } = useTranslation();
  const { redirect } = useSearch({ from: "/login" });
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const id = useId();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [needsTotp, setNeedsTotp] = useState(false);
  const [message, setMessage] = useState<Message | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  const totpField = useRef<HTMLInputElement>(null);

  useEffect(() => {
    void i18n.changeLanguage(browserLanguage()); // before sign-in, the browser's language
  }, [i18n]);
  useEffect(() => {
    if (needsTotp) {
      totpField.current?.focus();
    }
  }, [needsTotp]);

  const login = useMutation({
    mutationFn: (body: LoginIn) => apiFetch<undefined>("/api/auth/login", { method: "POST", body: JSON.stringify(body) }),
    meta: { authCall: true },
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: SESSION_KEY });
      await navigate({ href: safeRedirect(redirect), replace: true });
    },
    onError: (error) => {
      if (isProblem(error, "totp_required")) {
        setNeedsTotp(true);
        setMessage({ kind: "info", key: "login.totp_required" });
      } else if (isProblem(error, "unauthenticated")) {
        setTotp("");
        setMessage({ kind: "error", key: "login.invalid" });
      } else if (isProblem(error, "throttled")) {
        setMessage(throttledMessage(error));
      } else {
        setFailure(error);
      }
    },
  });

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    setMessage(null);
    setFailure(null);
    if (username === "" || password === "") {
      setMessage({ kind: "error", key: "login.missing" });
      return;
    }
    if (needsTotp && !TOTP_CODE.test(totp)) {
      setMessage({ kind: "error", key: "login.totp_format" });
      totpField.current?.focus();
      return;
    }
    login.mutate(needsTotp ? { username, password, totp } : { username, password });
  };

  const messageId = `${id}-message`;
  const hintId = `${id}-totp-hint`;
  const describedBy = message === null ? undefined : messageId;
  const invalid = message?.kind === "error";
  return (
    <main className={styles.page}>
      <div className={styles.card}>
        <p className={styles.brand}>{t("app.name")}</p>
        <h1 className={styles.title}>{t("login.title")}</h1>
        <form className={styles.form} noValidate onSubmit={submit}>
          <div className={styles.field}>
            <label htmlFor={`${id}-username`}>{t("login.username")}</label>
            <input id={`${id}-username`} name="username" type="text" autoComplete="username" autoCapitalize="none"
              spellCheck={false} value={username} aria-invalid={invalid} aria-describedby={describedBy}
              onChange={(event) => { setUsername(event.target.value); }} />
          </div>
          <div className={styles.field}>
            <label htmlFor={`${id}-password`}>{t("login.password")}</label>
            <input id={`${id}-password`} name="password" type="password" autoComplete="current-password"
              value={password} aria-invalid={invalid} aria-describedby={describedBy}
              onChange={(event) => { setPassword(event.target.value); }} />
          </div>
          {needsTotp && (
            <div className={styles.field}>
              <label htmlFor={`${id}-totp`}>{t("login.totp")}</label>
              <input ref={totpField} id={`${id}-totp`} name="totp" type="text" inputMode="numeric"
                autoComplete="one-time-code" maxLength={6} className={styles.code} value={totp}
                aria-invalid={message?.key === "login.totp_format"}
                aria-describedby={[hintId, describedBy].filter(Boolean).join(" ")}
                onChange={(event) => { setTotp(event.target.value.trim()); }} />
              <p id={hintId} className={styles.hint}>{t("login.totp_hint")}</p>
            </div>
          )}
          {message !== null && (
            <p id={messageId} role={message.kind === "error" ? "alert" : "status"}
              className={message.kind === "error" ? styles.error : styles.info}>
              {message.minutes === undefined ? t(message.key) : t(message.key, { minutes: message.minutes })}
            </p>
          )}
          {failure !== null && <ProblemBox error={failure} />}
          <button type="submit" className={styles.submit} disabled={login.isPending}>
            {login.isPending ? t("login.submitting") : t("login.submit")}
          </button>
        </form>
        <p className={styles.note}>{t("login.reset_note")}</p>
      </div>
    </main>
  );
}
