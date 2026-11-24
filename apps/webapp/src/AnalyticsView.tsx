import { useEffect, useState } from "react";

import { PERIODS, SECTIONS, type AnalyticsChoice, type Period, type Section } from "./analytics";
import { ApiError, getJson, type Failure } from "./api";
import { Chart, COLORS } from "./charts";
import { ModerationDashboard, type ModerationFigures } from "./ModerationDashboard";
import { RevenueDashboard, type Revenue } from "./RevenueDashboard";

export type Bucket = {
  start: string;
  signups: number;
  onboarded: number;
  vip_purchases: number;
  active: number;
  active_7d: number;
  active_30d: number;
};

export interface Growth {
  period: Period;
  bucket_days: number;
  buckets: Bucket[];
  totals: {
    by_status: Record<string, number>;
    onboarded: number;
    vip_now: number;
    active_today: number;
    active_7d: number;
    active_30d: number;
  };
}

type Loaded =
  | { section: "growth"; data: Growth }
  | { section: "moderation"; data: ModerationFigures }
  | { section: "revenue"; data: Revenue };

/** Growth totals; no individual is shown. */
function GrowthDashboard({ data }: { data: Growth }) {
  const weekly = data.bucket_days > 1;
  const totals = data.totals;
  return (
    <>
      <ul className="figures" aria-label="Now">
        <li>
          <strong>{totals.active_today}</strong> active today
        </li>
        <li>
          <strong>{totals.active_7d}</strong> active in the last 7 days
        </li>
        <li>
          <strong>{totals.active_30d}</strong> active in the last 30 days
        </li>
        <li>
          <strong>{totals.onboarded}</strong> onboarded accounts
        </li>
        <li>
          <strong>{totals.vip_now}</strong> VIP now
        </li>
        {Object.entries(totals.by_status).map(([status, count]) => (
          <li key={status}>
            <strong>{count}</strong> {status}
          </li>
        ))}
      </ul>
      <Chart
        title="Sign-ups and finished onboardings"
        data={data.buckets}
        weekly={weekly}
        series={[
          { key: "signups", label: "Sign-ups", color: COLORS.primary },
          { key: "onboarded", label: "Finished onboarding", color: COLORS.second },
        ]}
      />
      <p className="hint">
        Finished onboardings count only accounts that still exist. Activity is counted from when daily records
        began.
      </p>
      <Chart
        title="Active users"
        data={data.buckets}
        weekly={weekly}
        series={[
          { key: "active", label: weekly ? "Active in the week" : "Active that day", color: COLORS.primary },
          { key: "active_7d", label: "Active in the last 7 days", color: COLORS.second },
          { key: "active_30d", label: "Active in the last 30 days", color: COLORS.third },
        ]}
      />
      <Chart
        title="VIP passes bought"
        data={data.buckets}
        weekly={weekly}
        bars
        series={[{ key: "vip_purchases", label: "VIP passes", color: COLORS.primary }]}
      />
    </>
  );
}

interface Props {
  launchData: string;
  choice: AnalyticsChoice;
  onChoice: (choice: AnalyticsChoice) => void;
  onOpenAccount: (accountId: number) => void;
  onFailure: (failure: Failure) => void;
}

/** Admins only: growth, moderation, and revenue dashboards (D-072). The server
 * audits each view. */
export function AnalyticsView({ launchData, choice, onChoice, onOpenAccount, onFailure }: Props) {
  const { section, period } = choice;
  const wanted = `${section} ${period}`;
  // Each result is tagged with what it was loaded for, so a stale one is never shown.
  const [loaded, setLoaded] = useState<{ for: string; value: Loaded } | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    const tag = `${section} ${period}`;
    getJson<Loaded["data"]>(`/api/moderation/analytics/${section}?period=${period}`, launchData)
      .then((data) => {
        if (active) setLoaded({ for: tag, value: { section, data } as Loaded });
      })
      .catch((error: unknown) => {
        if (!active) return;
        const failure = error instanceof ApiError ? error.failure : "error";
        if (failure === "reopen" || failure === "denied") onFailure(failure);
        else setFailed(tag);
      });
    return () => {
      active = false;
    };
  }, [launchData, section, period, onFailure, attempt]);

  const current = loaded?.for === wanted ? loaded.value : null;
  const showFailure = failed === wanted;
  return (
    <section aria-label="Analytics">
      <h2>Analytics</h2>
      <div className="buttons" role="group" aria-label="Dashboard">
        {(Object.entries(SECTIONS) as [Section, string][]).map(([value, text]) => (
          <button
            key={value}
            type="button"
            aria-pressed={section === value}
            onClick={() => {
              setFailed(null);
              onChoice({ ...choice, section: value });
            }}
          >
            {text}
          </button>
        ))}
      </div>
      <label className="form">
        Period
        <select
          value={period}
          onChange={(event) => {
            setFailed(null);
            onChoice({ ...choice, period: event.target.value as Period });
          }}
        >
          {Object.entries(PERIODS).map(([value, text]) => (
            <option key={value} value={value}>
              {text}
            </option>
          ))}
        </select>
      </label>
      {showFailure && (
        <div className="notice" role="alert">
          <p>The analytics couldn't be loaded.</p>
          <button
            type="button"
            className="link"
            onClick={() => {
              setFailed(null);
              setAttempt((count) => count + 1);
            }}
          >
            Try again
          </button>
        </div>
      )}
      {!current && !showFailure && <p className="hint">Loading…</p>}
      {current?.section === "growth" && <GrowthDashboard data={current.data} />}
      {current?.section === "moderation" && (
        <ModerationDashboard data={current.data} onOpenAccount={onOpenAccount} />
      )}
      {current?.section === "revenue" && <RevenueDashboard data={current.data} />}
    </section>
  );
}
