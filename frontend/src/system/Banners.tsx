// The banners (spec §15.1): the server computes them (`StatusOut.banners`, critical first); each is shown by
// its code, with its params, in the order received. A critical one is an alert, a warning or a notice is a
// polite status. Each is keyed by its code, so a poll updates a banner in place and its live region
// announces the change (`applying_settings`' n/N).
import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { ProblemBox } from "../components/ProblemBox";
import { useAnnounce } from "../components/LiveRegion";
import { cx } from "../components/cx";
import { useServerText } from "../i18n/index";
import { type BannerOut, type JsonValue, useRetryApply } from "./api";
import styles from "./Banners.module.css";
import { type Format, useFormat } from "./format";

type T = ReturnType<typeof useTranslation>["t"];
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
      return p.failed === true
        ? t("banner.applying_settings_failed")
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
