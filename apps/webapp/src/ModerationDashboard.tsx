import { Chart, COLORS } from "./charts";
import { REASONS, when } from "./labels";

export interface ModerationFigures {
  period: string;
  bucket_days: number;
  buckets: { start: string; reports: Record<string, number> }[];
  backlog: { open: number; urgent: number; oldest_open_at: string | null };
  handling: {
    closed: number;
    action_taken: number;
    no_action: number;
    median_seconds: number | null;
    longest_seconds: number | null;
  };
  moderators: { nickname: string | null; public_id: string | null; actions: Record<string, number>; total: number }[];
  most_reported: {
    id: number;
    nickname: string | null;
    public_id: string | null;
    status: string;
    reports: number;
    open: number;
    findable: boolean;
  }[];
}

const REASON_COLORS = [COLORS.primary, COLORS.second, COLORS.third, COLORS.fourth, COLORS.fifth, COLORS.sixth];

const ACTION_COLUMNS: [string, string][] = [
  ["warn", "Warnings"],
  ["suspend", "Suspensions"],
  ["ban", "Bans"],
  ["lift", "Lifted"],
  ["close", "Closed"],
];

/** A length of time, roughly: "45 min", "3 h 20 min", "2 d 4 h". */
export function duration(seconds: number): string {
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  const days = Math.floor(hours / 24);
  return hours % 24 ? `${days} d ${hours % 24} h` : `${days} d`;
}

/** An account as listed: lookup can't find a deleted one, so it has no name. */
function name(nickname: string | null, publicId: string | null): string {
  if (nickname === null) return "Deleted account";
  return publicId ? `${nickname} · ${publicId}` : nickname;
}

/** Reports, decisions, and actions. Admins only (D-072). */
export function ModerationDashboard({
  data,
  onOpenAccount,
}: {
  data: ModerationFigures;
  onOpenAccount: (accountId: number) => void;
}) {
  const weekly = data.bucket_days > 1;
  const { backlog, handling } = data;
  const rows = data.buckets.map((bucket) => ({ start: bucket.start, ...bucket.reports }));
  return (
    <>
      <ul className="figures" aria-label="Now">
        <li>
          <strong>{backlog.open}</strong> open reports
        </li>
        <li>
          <strong>{backlog.urgent}</strong> urgent and open
        </li>
        <li>
          <strong>{backlog.oldest_open_at ? when(backlog.oldest_open_at) : "None"}</strong> oldest open report
        </li>
      </ul>
      <ul className="figures" aria-label="Decisions in the period">
        <li>
          <strong>{handling.closed}</strong> reports closed
        </li>
        <li>
          <strong>{handling.action_taken}</strong> with action taken
        </li>
        <li>
          <strong>{handling.no_action}</strong> with no action
        </li>
        <li>
          <strong>{handling.median_seconds === null ? "—" : duration(handling.median_seconds)}</strong> median time to a
          decision
        </li>
        <li>
          <strong>{handling.longest_seconds === null ? "—" : duration(handling.longest_seconds)}</strong> longest time to
          a decision
        </li>
      </ul>
      <Chart
        title="Reports by reason"
        data={rows}
        weekly={weekly}
        bars
        series={Object.entries(REASONS).map(([key, label], index) => ({
          key,
          label,
          color: REASON_COLORS[index % REASON_COLORS.length] ?? COLORS.primary,
        }))}
      />

      <section className="chart" aria-label="Actions by moderator">
        <h3>Actions by moderator</h3>
        {data.moderators.length === 0 ? (
          <p className="hint">No moderation actions in this period.</p>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Moderator</th>
                {ACTION_COLUMNS.map(([kind, label]) => (
                  <th key={kind} scope="col">
                    {label}
                  </th>
                ))}
                <th scope="col">Total</th>
              </tr>
            </thead>
            <tbody>
              {data.moderators.map((moderator, index) => (
                <tr key={moderator.public_id ?? `deleted-${index}`}>
                  <th scope="row">{name(moderator.nickname, moderator.public_id)}</th>
                  {ACTION_COLUMNS.map(([kind]) => (
                    <td key={kind}>{moderator.actions[kind] ?? 0}</td>
                  ))}
                  <td>{moderator.total}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="chart" aria-label="Most-reported accounts">
        <h3>Most-reported accounts</h3>
        <p className="hint">By reports made in this period.</p>
        {data.most_reported.length === 0 ? (
          <p className="hint">No reports in this period.</p>
        ) : (
          <ul className="list">
            {data.most_reported.map((account) => {
              const content = (
                <>
                  <span className="row-title">{name(account.nickname, account.public_id)}</span>
                  <span className="row-meta">
                    {account.reports} {account.reports === 1 ? "report" : "reports"} · {account.open} open ·{" "}
                    {account.status}
                  </span>
                </>
              );
              return (
                <li key={account.id}>
                  {account.findable ? (
                    <button type="button" className="row" onClick={() => onOpenAccount(account.id)}>
                      {content}
                    </button>
                  ) : (
                    <div className="row">{content}</div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </>
  );
}
