import { useEffect, useRef, useState } from "react";

import { ApiError, postJson, type Failure, type Item, type MediaBody, type ReasonBody, type Thread } from "./api";
import { when } from "./labels";
import { Message } from "./ReportView";

interface Props {
  launchData: string;
  accountId: number;
  onFailure: (failure: Failure) => void;
}

const MIN_REASON = 5;
const MAX_REASON = 300;

/** Admins only: the account's stored chats and direct messages, opened one at a time,
 * each time with the reason given here (saved in the audit log). */
export function AdminThreads({ launchData, accountId, onFailure }: Props) {
  const [reason, setReason] = useState("");
  const [threads, setThreads] = useState<Thread[] | null>(null);
  const [open, setOpen] = useState<{ thread: Thread; items: Item[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  async function call<T>(path: string, extra: object, done: (value: T) => void, failed: string) {
    setBusy(true);
    setNotice(null);
    try {
      const body: ReasonBody = { ...extra, reason: reason.trim() };
      const value = await postJson<T>(path, launchData, body);
      if (mounted.current) done(value);
    } catch (error) {
      if (!mounted.current) return;
      const failure = error instanceof ApiError ? error.failure : "error";
      if (failure === "reopen" || failure === "denied") onFailure(failure);
      else if (failure === "telegram") setNotice("Telegram couldn't send it. Please try again.");
      else setNotice(failed);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  const base = `/api/moderation/accounts/${accountId}`;
  const reasonValid = reason.trim().length >= MIN_REASON;

  if (threads === null) {
    return (
      <section className="actions" aria-label="Stored chats">
        <h3>Stored chats (admins only)</h3>
        <form
          className="form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!reasonValid) {
              setNotice(`Give a reason of at least ${MIN_REASON} characters.`);
              return;
            }
            void call<Thread[]>(`${base}/threads`, {}, setThreads, "The chats couldn't be listed.");
          }}
        >
          <label>
            Reason (saved in the audit log)
            <textarea
              value={reason}
              maxLength={MAX_REASON}
              rows={3}
              onChange={(event) => {
                setReason(event.target.value);
                setNotice(null);
              }}
            />
          </label>
          <button type="submit" className="primary" disabled={busy}>
            Open stored chats
          </button>
        </form>
        {notice && (
          <p className="notice" role="alert">
            {notice}
          </p>
        )}
      </section>
    );
  }

  function view(item: Item) {
    const body: MediaBody = { source: item.source, item_id: item.id };
    void call<unknown>(
      `${base}/media`,
      body,
      () => setNotice("Sent to your Telegram chat. It's queued for deletion after 10 minutes."),
      "That item is no longer available.",
    );
  }

  return (
    <section className="actions" aria-label="Stored chats">
      <h3>Stored chats (admins only)</h3>
      <p className="hint">Reason: {reason.trim()}</p>
      {open === null ? (
        <>
          {threads.length === 0 && <p className="hint">No stored chats or direct messages.</p>}
          <ul className="list">
            {threads.map((thread) => (
              <li key={`${thread.kind}-${thread.ref}`}>
                <button
                  type="button"
                  className="row"
                  disabled={busy}
                  onClick={() =>
                    void call<Item[]>(
                      `${base}/threads/open`,
                      { kind: thread.kind, ref: thread.ref },
                      (items) => setOpen({ thread, items }),
                      "That chat couldn't be opened.",
                    )
                  }
                >
                  <span className="row-title">
                    {thread.kind === "chat" ? "Chat" : "Direct messages"} with {thread.partner_nickname ?? "a deleted account"}
                  </span>
                  <span className="row-meta">
                    {thread.messages} message{thread.messages === 1 ? "" : "s"} · {when(thread.first_at)} – {when(thread.last_at)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <>
          <button type="button" className="link" onClick={() => setOpen(null)}>
            ◀ All stored chats
          </button>
          <ul className="messages" aria-label="Messages">
            {open.items.map((item) => (
              <Message key={`${item.source}-${item.id}`} item={item} disabled={busy} onView={view} />
            ))}
          </ul>
        </>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
    </section>
  );
}
