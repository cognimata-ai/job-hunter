// The banners (spec §15.1): the server computes them (`StatusOut.banners`, critical first); each is shown by
// its code, with its params, in the order received. A critical one is an alert, a warning or a notice is a
// polite status. Each is keyed by its code, so a poll updates a banner in place and its live region
// announces the change (`applying_settings`' n/N). "Deletion not yet confirmed offsite" (`deletion_unshipped`,
// warn; plan M5 Task 20) links to the deletions' status on Privacy & data.
import { Link } from "@tanstack/react-router";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { ProblemBox } from "../components/ProblemBox";
import { useAnnounce } from "../components/LiveRegion";
import { cx } from "../components/cx";
import { useServerText } from "../i18n/index";
import { type BannerOut, type JsonValue, useRetryApply } from "./api";
import styles from "./Banners.module.css";
import { type Format, useFormat } from "./format";

// The `t` of `useTranslation()` (the default namespace, no key prefix). Not `ReturnType<typeof useTranslation>`,
// which instantiates the hook's key prefix with every prefix of the catalogue: too deep for the compiler once the
// catalogue grows (TS2589, plan M4 Task 27).
type T = TFunction;
type ServerText = ReturnType<typeof useServerText>;

// The pauses whose cause the System section's Needs attention list holds (Task 31).
const NEEDS_ATTENTION: ReadonlySet<string> = new Set(["cap_reached", "reservation_bound_violated", "scoring_paused"]);

function count(value: JsonValue): number {
  return typeof value === "number" ? value : 0;
}

function text(value: JsonValue): string {
  return typeof value === "string" ? value : "";
}

/** A code this client does not know (a newer server): shown as its code. */
function unknownCode(code: never): string {
  return String(code);
}

function bannerText(banner: BannerOut, t: T, serverText: ServerText, fmt: Format): string {
  const p = banner.params;
  switch (banner.code) {
    case "stale_data":
    case "cap_reached":
    case "reservation_bound_violated":
    case "scope_not_applied":
    case "deletion_unshipped":
      return t(`banner.${banner.code}`);
    case "scoring_paused":
      return t("banner.scoring_paused", { reason: serverText("pause", text(p.reason)) });
    case "critical_alerts":
      return t("banner.critical_alerts", { critical: fmt.number(count(p.count)) });
    case "paid_search_paused":
      return t("banner.paid_search_paused", {
        until: typeof p.until === "string" ? fmt.dayMonthTime(p.until) : t("value.none"),
        reason: serverText("paid_search_reason", text(p.reason)),
      });
    case "applying_settings":
      if (p.failed === true) {
        return t("banner.applying_settings_failed");
      }
      // no counts before the apply reports any (status.py: "a count it has not reported yet is 0")
      return count(p.total) === 0
        ? t("banner.applying_settings_starting")
        : t("banner.applying_settings", { done: fmt.number(count(p.done)), total: fmt.number(count(p.total)) });
    case "rescoring":
      return t("banner.rescoring", { done: fmt.number(count(p.done)), total: fmt.number(count(p.total)) });
    default:
      return serverText("banner", unknownCode(banner.code));
  }
}

function RetryApply() {
  const { t } = useTranslation();
  const announce = useAnnounce();
  const retry = useRetryApply();
  return (
    <>
      <button type="button" className={styles.action} disabled={retry.isPending}
        onClick={() => {
          retry.mutate(undefined, { onSuccess: () => { announce(t("banner.retry_apply_started")); } });
        }}>
        {t("banner.retry_apply")}
      </button>
      {retry.isError && <div className={styles.problem}><ProblemBox error={retry.error} /></div>}
    </>
  );
}

function Banner({ banner }: { banner: BannerOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const fmt = useFormat();
  const severity = banner.severity === "critical" || banner.severity === "warn" ? banner.severity : "info";
  return (
    <div role={severity === "critical" ? "alert" : "status"} className={cx(styles.banner, styles[severity])}>
      <p className={styles.text}>{bannerText(banner, t, serverText, fmt)}</p>
      {NEEDS_ATTENTION.has(banner.code) && (
        <>
          {" "}
          <Link to="/settings" hash="system" className={styles.link}>{t("banner.needs_attention")}</Link>
        </>
      )}
      {banner.code === "scope_not_applied" && <>{" "}<RetryApply /></>}
      {banner.code === "deletion_unshipped" && (
        <>
          {" "}
          <Link to="/settings" hash="privacy" className={styles.link}>{t("banner.deletion_status")}</Link>
        </>
      )}
    </div>
  );
}

export function Banners({ banners }: { banners: BannerOut[] }) {
  if (banners.length === 0) {
    return null;
  }
  return (
    <div className={styles.banners}>
      {banners.map((banner) => <Banner key={banner.code} banner={banner} />)}
    </div>
  );
}
