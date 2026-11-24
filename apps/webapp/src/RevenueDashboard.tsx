import { Chart, COLORS } from "./charts";

export interface RevenueBucket {
  start: string;
  coin_stars: number;
  vip_stars: number;
  coin_packs: number;
  vip_passes: number;
  first_vip: number;
  refunds: Record<string, number>;
  refunded_stars: number;
}

export interface Revenue {
  period: string;
  bucket_days: number;
  buckets: RevenueBucket[];
  totals: {
    stars: number;
    refunds_due: number;
    refunds_due_stars: number;
    vip_buyers: number;
    repeat_vip_buyers: number;
    onboarded: number;
  };
}

function percent(part: number, whole: number): string {
  if (whole === 0) return "—";
  return `${((part / whole) * 100).toFixed(1)}%`;
}

/** Stars paid, refunds, and VIP conversion. Admins only (D-072). */
export function RevenueDashboard({ data }: { data: Revenue }) {
  const weekly = data.bucket_days > 1;
  const { totals } = data;
  const rows = data.buckets.map(({ refunds, ...bucket }) => ({ ...bucket, ...refunds }));
  const sum = (key: "coin_stars" | "vip_stars" | "refunded_stars") =>
    data.buckets.reduce((total, bucket) => total + bucket[key], 0);
  return (
    <>
      <ul className="figures" aria-label="In the period">
        <li>
          <strong>⭐ {sum("coin_stars") + sum("vip_stars")}</strong> Stars paid and kept
        </li>
        <li>
          <strong>⭐ {sum("refunded_stars")}</strong> Stars refunded
        </li>
      </ul>
      <ul className="figures" aria-label="All time">
        <li>
          <strong>⭐ {totals.stars}</strong> Stars paid and kept, all time
        </li>
        <li>
          <strong>{percent(totals.vip_buyers, totals.onboarded)}</strong> of onboarded accounts have bought VIP (
          {totals.vip_buyers} of {totals.onboarded})
        </li>
        <li>
          <strong>{totals.repeat_vip_buyers}</strong> bought VIP more than once
        </li>
        <li>
          <strong>{totals.refunds_due}</strong> refunds due (⭐ {totals.refunds_due_stars})
        </li>
      </ul>
      <p className="hint">
        Stars are what users paid through Telegram, not earnings: Telegram takes its share when Stars are withdrawn.
        Purchases count on the day they were made, leaving out any refunded or due for a refund; refunds count on the
        day they were made.
      </p>
      <Chart
        title="Stars paid"
        data={rows}
        weekly={weekly}
        bars
        series={[
          { key: "coin_stars", label: "Coin packs", color: COLORS.primary },
          { key: "vip_stars", label: "VIP passes", color: COLORS.third },
        ]}
      />
      <Chart
        title="Purchases"
        data={rows}
        weekly={weekly}
        bars
        series={[
          { key: "coin_packs", label: "Coin packs", color: COLORS.primary },
          { key: "vip_passes", label: "VIP passes", color: COLORS.third },
        ]}
      />
      <Chart
        title="First-time VIP buyers"
        data={rows}
        weekly={weekly}
        bars
        series={[{ key: "first_vip", label: "First-time buyers", color: COLORS.second }]}
      />
      <Chart
        title="Refunds"
        data={rows}
        weekly={weekly}
        bars
        series={[
          { key: "admin", label: "By an admin", color: COLORS.primary },
          { key: "telegram", label: "By Telegram", color: COLORS.third },
          { key: "account_deleted", label: "Account deleted", color: COLORS.sixth },
        ]}
      />
      <Chart
        title="Stars refunded"
        data={rows}
        weekly={weekly}
        bars
        series={[{ key: "refunded_stars", label: "Stars refunded", color: COLORS.fifth }]}
      />
    </>
  );
}
