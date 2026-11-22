import { useEffect, useState } from "react";

import { ActionPanel } from "./ActionPanel";
import { AdminThreads } from "./AdminThreads";
import { ApiError, getJson, type Account, type AccountActionKind, type Failure, type ReportSummary } from "./api";
import { ACTIONS, REASONS, RULES, when } from "./labels";
import { Card } from "./ReportView";

interface Props {
  launchData: string;
  accountId: number;
  isAdmin: boolean;
  /** A notice from an action taken on one of the account's reports. */
  notice: string | null;
  onOpenReport: (reportId: number) => void;
  onFailure: (failure: Failure) => void;
}

function Reports({ title, reports, onOpen }: { title: string; reports: ReportSummary[]; onOpen: (id: number) => void }) {
  return (
    <section aria-label={title}>
      <h3>{title}</h3>
      {reports.length === 0 ? (
        <p className="hint">None.</p>
      ) : (
        <ul className="list">
          {reports.map((report) => (
            <li key={report.id}>
              <button type="button" className="row" onClick={() => onOpen(report.id)}>
                <span className="row-title">{REASONS[report.reason] ?? report.reason}</span>
                <span className="row-meta">
                  {report.kind === "chat" ? "Chat" : "Profile"} · {report.status}
                  {report.outcome ? ` (${report.outcome === "action_taken" ? "action taken" : "no action"})` : ""} ·{" "}
                  {when(report.created_at)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** One account: its card, status, record, reports, actions, and, for admins, its
 * stored chats. */
export function AccountView({ launchData, accountId, isAdmin, notice: reportNotice, onOpenReport, onFailure }: Props) {
  const [data, setData] = useState<Account | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getJson<Account>(`/api/moderation/accounts/${accountId}`, launchData)
      .then((loaded) => {
        if (active) setData(loaded);
      })
      .catch((error: unknown) => {
        if (!active) return;
        const failure = error instanceof ApiError ? error.failure : "error";
        if (failure === "reopen" || failure === "denied" || failure === "notFound") onFailure(failure);
        else setFailed(true);
      });
    return () => {
      active = false;
    };
  }, [launchData, accountId, onFailure, attempt]);

  if (failed) {
    return (
      <div className="notice" role="alert">
        <p>This account couldn't be loaded.</p>
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
    );
  }
  if (data === null) return <p className="hint">Loading…</p>;

  // Moderators can lift a suspension; only admins can lift a ban.
  const liftable = data.person.status === "suspended" || (data.person.status === "banned" && isAdmin);
  const kinds: AccountActionKind[] = liftable ? ["warn", "suspend", "ban", "lift"] : ["warn", "suspend", "ban"];
  const shown = notice ?? reportNotice;
  return (
    <article aria-label={`Account ${data.id}`}>
      <Card title="Account" person={data.person} />
      {data.suspended_until && <p className="hint">Suspended until {when(data.suspended_until)}</p>}
      {shown && (
        <p className="notice" role="status">
          {shown}
        </p>
      )}

      <section aria-label="Record">
        <h3>Record</h3>
        <p>
          Reports received: {data.record.reports_received} ({data.record.reports_open} open)
        </p>
        {data.record.actions.length === 0 ? (
          <p className="hint">No past warnings, suspensions, or bans.</p>
        ) : (
          <ul className="list">
            {data.record.actions.map((action, index) => (
              <li key={index}>
                {ACTIONS[action.kind] ?? action.kind}
                {action.days ? ` (${action.days} days${action.ends_at ? `, until ${when(action.ends_at)}` : ""})` : ""}
                {action.rule ? ` · ${RULES[action.rule] ?? action.rule}` : ""} · {when(action.created_at)}
              </li>
            ))}
          </ul>
        )}
      </section>

      <Reports title="Reports received" reports={data.reports_received} onOpen={onOpenReport} />
      <Reports title="Reports made" reports={data.reports_made} onOpen={onOpenReport} />

      <ActionPanel
        key={attempt}
        launchData={launchData}
        path={`/api/moderation/accounts/${data.id}/action`}
        kinds={kinds}
        closesReport={false}
        nickname={data.person.nickname ?? "this account"}
        onDone={(done) => {
          setNotice(done);
          setAttempt((count) => count + 1); // reload the status and record
        }}
        onFailure={onFailure}
      />

      {isAdmin && <AdminThreads launchData={launchData} accountId={data.id} onFailure={onFailure} />}
    </article>
  );
}
