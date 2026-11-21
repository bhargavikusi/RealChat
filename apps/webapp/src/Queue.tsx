import { useEffect, useState } from "react";

import { ApiError, getJson, type Failure, type QueueItem } from "./api";
import { REASONS, when } from "./labels";

interface Props {
  launchData: string;
  notice: string | null;
  onOpen: (reportId: number) => void;
  onFailure: (failure: Failure) => void;
}

/** Open reports, urgent first, then oldest first. */
export function Queue({ launchData, notice, onOpen, onFailure }: Props) {
  const [items, setItems] = useState<QueueItem[] | null>(null);

  useEffect(() => {
    getJson<QueueItem[]>("/api/moderation/reports", launchData)
      .then(setItems)
      .catch((error: unknown) => onFailure(error instanceof ApiError ? error.failure : "error"));
  }, [launchData, onFailure]);

  return (
    <section aria-label="Open reports">
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      <h2>Open reports</h2>
      {items === null && <p className="hint">Loading…</p>}
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
