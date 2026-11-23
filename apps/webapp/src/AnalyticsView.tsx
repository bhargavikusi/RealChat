import { useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { ApiError, getJson, type Failure } from "./api";

export type Period = "7d" | "30d" | "90d" | "12m";

export interface Bucket {
  start: string;
  signups: number;
  onboarded: number;
  vip_purchases: number;
  active: number;
  active_7d: number;
  active_30d: number;
}

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

const PERIODS: Record<Period, string> = {
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  "90d": "Last 90 days",
  "12m": "Last 12 months (by week)",
};

// Provisional colours, until the design pass.
const COLORS = { primary: "#2481cc", second: "#34a853", third: "#f29900" };

interface Series {
  key: keyof Bucket;
  label: string;
  color: string;
}

function label(start: string, weekly: boolean): string {
  const day = new Date(`${start}T00:00:00`);
  const text = day.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  return weekly ? `Week of ${text}` : text;
}

/** A chart with a "Show data" table under it, for screen readers and exact numbers. */
function Chart({
  title,
  data,
  series,
  weekly,
  bars = false,
}: {
  title: string;
  data: Bucket[];
  series: Series[];
  weekly: boolean;
  bars?: boolean;
}) {
  const rows = data.map((bucket) => ({ ...bucket, label: label(bucket.start, weekly) }));
  const axes = (
    <>
      <CartesianGrid strokeDasharray="3 3" />
      <XAxis dataKey="label" fontSize={11} minTickGap={16} />
      <YAxis allowDecimals={false} fontSize={11} width={32} />
      <Tooltip />
      {series.length > 1 && <Legend />}
    </>
  );
  return (
    <section className="chart" aria-label={title}>
      <h3>{title}</h3>
      <div aria-hidden="true">
        <ResponsiveContainer width="100%" height={220}>
          {bars ? (
            <BarChart data={rows}>
              {axes}
              {series.map((item) => (
                <Bar key={item.key} dataKey={item.key} name={item.label} fill={item.color} />
              ))}
            </BarChart>
          ) : (
            <LineChart data={rows}>
              {axes}
              {series.map((item) => (
                <Line
                  key={item.key}
                  type="monotone"
                  dataKey={item.key}
                  name={item.label}
                  stroke={item.color}
                  dot={false}
                  isAnimationActive={false}
                />
              ))}
            </LineChart>
          )}
        </ResponsiveContainer>
      </div>
      <details>
        <summary>Show data</summary>
        <table className="data">
          <thead>
            <tr>
              <th scope="col">{weekly ? "Week" : "Day"}</th>
              {series.map((item) => (
                <th key={item.key} scope="col">
                  {item.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.start}>
                <th scope="row">{row.label}</th>
                {series.map((item) => (
                  <td key={item.key}>{row[item.key]}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </section>
  );
}

interface Props {
  launchData: string;
  onFailure: (failure: Failure) => void;
}

/** Admins only: growth and activity totals (D-072). No individual is shown. */
export function AnalyticsView({ launchData, onFailure }: Props) {
  const [period, setPeriod] = useState<Period>("30d");
  const [data, setData] = useState<Growth | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    getJson<Growth>(`/api/moderation/analytics/growth?period=${period}`, launchData)
      .then((loaded) => {
        if (active) setData(loaded);
      })
      .catch((error: unknown) => {
        if (!active) return;
        const failure = error instanceof ApiError ? error.failure : "error";
        if (failure === "reopen" || failure === "denied") onFailure(failure);
        else setFailed(true);
      });
    return () => {
      active = false;
    };
  }, [launchData, period, onFailure, attempt]);

  const weekly = (data?.bucket_days ?? 1) > 1;
  const totals = data?.totals;
  return (
    <section aria-label="Analytics">
      <h2>Growth and activity</h2>
      <label className="form">
        Period
        <select
          value={period}
          onChange={(event) => {
            setData(null);
            setFailed(false);
            setPeriod(event.target.value as Period);
          }}
        >
          {Object.entries(PERIODS).map(([value, text]) => (
            <option key={value} value={value}>
              {text}
            </option>
          ))}
        </select>
      </label>
      {failed && (
        <div className="notice" role="alert">
          <p>The analytics couldn't be loaded.</p>
          <button
            type="button"
            className="link"
            onClick={() => {
              setFailed(false);
              setAttempt((count) => count + 1);
            }}
          >
            Try again
          </button>
        </div>
      )}
      {!data && !failed && <p className="hint">Loading…</p>}
      {data && totals && (
        <>
          <ul className="figures" aria-label="Now">
            <li>
              <strong>{totals.active_today}</strong> active today
            </li>
            <li>
              <strong>{totals.active_7d}</strong> active in 7 days
            </li>
            <li>
              <strong>{totals.active_30d}</strong> active in 30 days
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
            Finished onboardings count only accounts that still exist. Activity is counted from when daily
            records began.
          </p>
          <Chart
            title="Active users"
            data={data.buckets}
            weekly={weekly}
            series={[
              { key: "active", label: weekly ? "Active in the week" : "Active that day", color: COLORS.primary },
              { key: "active_7d", label: "Active in 7 days", color: COLORS.second },
              { key: "active_30d", label: "Active in 30 days", color: COLORS.third },
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
      )}
    </section>
  );
}
