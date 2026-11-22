import { useEffect, useState } from "react";

import { ApiError, getJson, type Failure, type QueueItem } from "./api";
import { REASONS, when } from "./labels";

interface Props {
  launchData: string;
  notice: string | null;
  onOpen: (reportId: number) => void;
  /** Sign-in problems, which end the session: reopen from the bot, or no access. */
  onFailure: (failure: Failure) => void;
}

/** Open reports, urgent first, then oldest first. */
export function Queue({ launchData, notice, onOpen, onFailure }: Props) {
  const [items, setItems] = useState<QueueItem[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true; // a late answer after leaving the queue is ignored
    getJson<QueueItem[]>("/api/moderation/reports", launchData)
      .then((loaded) => {
        if (active) setItems(loaded);
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
  }, [launchData, onFailure, attempt]);

  return (
    <section aria-label="Open reports">
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      <h2>Open reports</h2>
      {failed && (
        <div className="notice" role="alert">
          <p>The reports couldn't be loaded.</p>
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
      {items === null && !failed && <p className="hint">Loading…</p>}
      {items?.length === 0 && <p className="hint">No open reports.</p>}
      <ul className="list">
        {items?.map((item) => (
          <li key={item.id}>
            <button type="button" className="row" onClick={() => onOpen(item.id)}>
              <span className="row-title">
                {item.urgent && <span className="badge urgent">Urgent</span>}
                {REASONS[item.reason] ?? item.reason}
              </span>
              <span className="row-meta">
                {item.kind === "chat" ? "Chat" : "Profile"} · {item.reported_nickname ?? "Deleted account"} ·{" "}
                {when(item.created_at)}
                {item.has_details ? " · details" : ""}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
