// Budget & quota (spec §6.9, §15.7; plan M4 Task 28, resolution g): the LLM cap, the non-scoring reserve, the
// overrun buffer, the bucket floor, the most and fewest targets per cycle and the batch options (the `llm` group
// and the scoring group's budget fields); then the live state, read-only and never part of the form: the month's
// counted spend and headroom, the USD bucket (balance, capacity, rate), `ĉ` and its source, the active bulk
// re-score's budget and spend, the amount a quarantined batch holds (with Needs attention), and each provider's
// quota window (usage, reset, reserved, ambiguous, adjustments). The live reads (`GET /api/system/spend`,
// `GET /api/system/quota`) are fresh for 60 s and read again every 60 s in the background while shown.
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { type ReactNode, useId } from "react";
import { useTranslation } from "react-i18next";
import { apiFetch } from "../api/client";
import type { components } from "../api/schema";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { intlLocale, useServerText } from "../i18n/index";
import { SPEND_KEY, type SpendOut } from "../jobs/api";
import { PROVIDERS } from "../system/api";
import { useFormat } from "../system/format";
import styles from "./BudgetSection.module.css";
import { usdFine } from "./DryRunPanel";
import { NumberField, ToggleField } from "./fields";
import fields from "./fields.module.css";

type QuotaDetailOut = components["schemas"]["QuotaDetailOut"];
type QuotaOut = Partial<Record<string, QuotaDetailOut | null>>;

export const QUOTA_KEY = ["system", "quota"] as const;
export const LIVE_MS = 60_000;

function useLiveSpend(): UseQueryResult<SpendOut> {
  return useQuery({
    queryKey: SPEND_KEY,
    queryFn: ({ signal }) => apiFetch<SpendOut>("/api/system/spend", { signal, background: true }),
    staleTime: LIVE_MS,
    refetchInterval: LIVE_MS,
  });
}

function useQuota(): UseQueryResult<QuotaOut> {
  return useQuery({
    queryKey: QUOTA_KEY,
    queryFn: ({ signal }) => apiFetch<QuotaOut>("/api/system/quota", { signal, background: true }),
    staleTime: LIVE_MS,
    refetchInterval: LIVE_MS,
  });
}

/** A live read's state: a skeleton, its failure with Retry, or `render` of its data. */
function Live<T>({ query, render }: { query: UseQueryResult<T>; render: (data: T) => ReactNode }) {
  if (query.isPending) {
    return <Skeleton variant="line" count={2} />;
  }
  if (query.isError) {
    return <ProblemBox error={query.error} onRetry={() => { void query.refetch(); }} />;
  }
  return <>{render(query.data)}</>;
}

function Spend({ spend }: { spend: SpendOut }) {
  const { t, i18n } = useTranslation();
  const format = useFormat();
  const serverText = useServerText();
  const fine = usdFine(intlLocale(i18n.language));
  const bulk = spend.bulk_operation;
  const quarantined = Number(spend.quarantined_batch_usd) > 0;
  return (
    <dl className={styles.figures}>
      <div className={styles.figure}>
        <dt>{t("settings_budget.counted")}</dt>
        <dd>{t("settings_budget.of_cap", { value: format.usd(spend.counted), cap: format.usd(spend.cap) })}</dd>
      </div>
      <div className={styles.figure}>
        <dt>{t("settings_budget.headroom")}</dt>
        <dd>{format.usd(spend.headroom)}</dd>
      </div>
      <div className={styles.figure}>
        <dt>{t("settings_budget.bucket")}</dt>
        <dd>
          {t("settings_budget.bucket_value", {
            usd: format.usd(spend.bucket.usd),
            capacity: format.usd(spend.bucket.capacity),
            rate: fine(spend.bucket.rate_per_h),
          })}
        </dd>
      </div>
      <div className={styles.figure}>
        <dt>{t("settings_budget.c_hat")}</dt>
        <dd>{t("settings_budget.c_hat_value", { value: fine(spend.c_hat), source: serverText("c_hat_source", spend.c_hat_source) })}</dd>
      </div>
      <div className={styles.figure}>
        <dt>{t("settings_budget.bulk")}</dt>
        <dd>
          {bulk !== null && bulk.state === "active"
            ? t("settings_budget.bulk_value", { spent: format.usd(bulk.spent_usd), budget: format.usd(bulk.budget_usd) })
            : t("settings_budget.bulk_none")}
        </dd>
      </div>
      <div className={styles.figure}>
        <dt>{t("settings_budget.quarantined")}</dt>
        <dd>
          {format.usd(spend.quarantined_batch_usd)}
          {quarantined && (
            <>
              {" · "}
              <a href="/settings#system" className={fields.link}>{t("banner.needs_attention")}</a>
            </>
          )}
        </dd>
      </div>
    </dl>
  );
}

function QuotaWindows({ quota, labelledBy }: { quota: QuotaOut; labelledBy: string }) {
  const { t } = useTranslation();
  const format = useFormat();
  const serverText = useServerText();
  return (
    <div className={styles.tableScroll}>
      <table className={styles.table} aria-labelledby={labelledBy}>
        <thead>
          <tr>
            <th scope="col">{t("settings_budget.quota_provider")}</th>
            <th scope="col">{t("settings_budget.quota_used")}</th>
            <th scope="col">{t("settings_budget.quota_resets")}</th>
            <th scope="col">{t("settings_budget.quota_reserved")}</th>
            <th scope="col">{t("settings_budget.quota_ambiguous")}</th>
            <th scope="col">{t("settings_budget.quota_adjustments")}</th>
          </tr>
        </thead>
        <tbody>
          {PROVIDERS.map((provider) => {
            const window = quota[provider] ?? null;
            const name = <th scope="row">{serverText("provider", provider)}</th>;
            if (window === null) {
              return (
                <tr key={provider}>
                  {name}
                  <td colSpan={5} className={styles.none}>{t("settings_budget.quota_none")}</td>
                </tr>
              );
            }
            return (
              <tr key={provider}>
                {name}
                <td>{t("settings_budget.quota_used_value", { used: format.number(window.used), limit: format.number(window.limit_units) })}</td>
                <td>{format.dayMonth(window.window_end)}</td>
                <td>{format.number(window.reserved)}</td>
                <td>{format.number(window.ambiguous)}</td>
                <td>{format.number(window.adjustments)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function BudgetSection() {
  const { t } = useTranslation();
  const spend = useLiveSpend();
  const quota = useQuota();
  const quotaHeading = useId();
  const usd = t("settings_budget.unit_usd");
  return (
    <div className={styles.budget}>
      <div className={fields.grid}>
        <NumberField path="llm.cap_usd" label={t("settings_budget.cap")} unit={usd} decimal />
        <NumberField path="llm.nonscoring_reserve_pct" label={t("settings_budget.reserve")} min={0} max={20}
          unit={t("settings_budget.unit_percent")} />
        <NumberField path="llm.overrun_buffer_usd" label={t("settings_budget.overrun_buffer")} unit={usd} decimal />
        <NumberField path="llm.min_bucket_capacity_usd" label={t("settings_budget.bucket_floor")} unit={usd} decimal />
        <NumberField path="scoring.max_targets_per_cycle" label={t("settings_budget.max_targets")} min={1} max={500} />
        <NumberField path="scoring.min_cycle_targets" label={t("settings_budget.min_targets")} min={1} max={100} />
      </div>
      <h3 className={fields.heading}>{t("settings_budget.batch")}</h3>
      <div className={fields.grid}>
        <ToggleField path="scoring.batch_bulk_rescore" label={t("settings_budget.batch_bulk")} />
        <NumberField path="scoring.batch_min_targets" label={t("settings_budget.batch_min")} min={20} max={100000} />
        <NumberField path="scoring.batch_max_items" label={t("settings_budget.batch_max")} min={200} max={10000} />
        <NumberField path="scoring.bulk_rescore_max_budget_share" label={t("settings_budget.bulk_share")} decimal
          hint={t("settings_budget.bulk_share_hint")} />
      </div>
      <div className={styles.live}>
        <h3 className={fields.heading}>{t("settings_budget.live")}</h3>
        <Live query={spend} render={(data) => <Spend spend={data} />} />
        <h3 id={quotaHeading} className={fields.heading}>{t("settings_budget.quota")}</h3>
        <Live query={quota} render={(data) => <QuotaWindows quota={data} labelledBy={quotaHeading} />} />
      </div>
    </div>
  );
}
