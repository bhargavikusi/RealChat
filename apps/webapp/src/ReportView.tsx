import { useEffect, useRef, useState } from "react";

import { ActionPanel } from "./ActionPanel";
import { ApiError, getJson, postJson, type Failure, type Item, type MediaBody, type Person, type Review } from "./api";
import { ACTIONS, KINDS, REASONS, RULES, when } from "./labels";

interface Props {
  launchData: string;
  reportId: number;
  onDone: (notice: string) => void;
  onFailure: (failure: Failure) => void;
}

export function Card({ title, person }: { title: string; person: Person }) {
  if (person.deleted) {
    return (
      <div className="card">
        <h3>{title}</h3>
        <p className="hint">Deleted account · {person.status}</p>
      </div>
    );
  }
  const details = [person.age, person.gender, person.country_code, person.city].filter(Boolean).join(" · ");
  return (
    <div className="card">
      <h3>{title}</h3>
      <p>
        <strong>{person.nickname}</strong> {person.public_id && <span className="hint">· ID {person.public_id}</span>}
      </p>
      {details && <p>{details}</p>}
      {person.bio && <p className="quote">{person.bio}</p>}
      <p className="hint">Status: {person.status}</p>
    </div>
  );
}

export function Message({ item, disabled, onView }: { item: Item; disabled: boolean; onView: (item: Item) => void }) {
  const markers = [item.private && "Private mode", item.view_once && "view once", item.edited_at && "edited"].filter(Boolean);
  return (
    <li className={`message ${item.sender}`}>
      <span className="row-meta">
        {item.sender === "reporter" ? "Reporter" : "Reported"} · {when(item.sent_at)}
        {markers.length > 0 && ` · ${markers.join(" · ")}`}
      </span>
      {item.kind !== "text" && <span className="kind">{KINDS[item.kind] ?? item.kind}</span>}
      {item.text && <span className="text">{item.text}</span>}
      {item.edited_text && <span className="text edited">Edited to: {item.edited_text}</span>}
      {item.viewable && (
        <button type="button" className="link" disabled={disabled} onClick={() => onView(item)}>
          📎 View in Telegram
        </button>
      )}
    </li>
  );
}

/** One report, with the people, the reported person's record, the content, and the
 * decision. */
export function ReportView({ launchData, reportId, onDone, onFailure }: Props) {
  const [data, setData] = useState<Review | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [sending, setSending] = useState(false);
  const [mediaNotice, setMediaNotice] = useState<string | null>(null);
  // Answers that arrive after leaving this report are ignored.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    getJson<Review>(`/api/moderation/reports/${reportId}`, launchData)
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
  }, [launchData, reportId, onFailure, attempt]);

  async function view(body: MediaBody) {
    if (sending) return;
    setSending(true);
    setMediaNotice(null);
    try {
      await postJson(`/api/moderation/reports/${reportId}/media`, launchData, body);
      if (mounted.current) setMediaNotice("Sent to your Telegram chat. It's queued for deletion after 10 minutes.");
    } catch (error) {
      if (!mounted.current) return;
      if (error instanceof ApiError && (error.failure === "reopen" || error.failure === "denied")) {
        onFailure(error.failure);
      } else if (error instanceof ApiError && error.failure === "telegram") {
        setMediaNotice("Telegram couldn't send it. Please try again.");
      } else {
        setMediaNotice("That item is no longer available.");
      }
    } finally {
      if (mounted.current) setSending(false);
    }
  }

  if (failed) {
    return (
      <div className="notice" role="alert">
        <p>This report couldn't be loaded.</p>
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
  const open = data.status === "open";
  return (
    <article aria-label={`Report ${data.id}`}>
      <h2>
        {data.urgent && <span className="badge urgent">Urgent</span>}
        {REASONS[data.reason] ?? data.reason}
      </h2>
      <p className="hint">
        {data.kind === "chat" ? "Chat report" : "Profile report"} · {when(data.created_at)}
        {!open && ` · closed (${data.outcome === "action_taken" ? "action taken" : "no action"})`}
      </p>
      {data.details && <p className="quote">“{data.details}”</p>}

      <div className="cards">
        <Card title="Reported" person={data.reported} />
        <Card title="Reporter" person={data.reporter} />
      </div>
      {data.reported.has_photo && (
        <button type="button" className="link" disabled={sending} onClick={() => void view({ source: "photo" })}>
          📎 View their profile photo in Telegram
        </button>
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

      <section aria-label={data.kind === "chat" ? "Chat" : "Direct contact"}>
        <h3>{data.kind === "chat" ? "Chat" : "Direct messages"}</h3>
        {data.messages.length === 0 && <p className="hint">No stored messages.</p>}
        <ul className="messages">
          {data.messages.map((item) => (
            <Message
              key={`${item.source}-${item.id}`}
              item={item}
              disabled={sending}
              onView={(chosen) => void view({ source: chosen.source, item_id: chosen.id })}
            />
          ))}
        </ul>
        {data.requests.length > 0 && (
          <>
            <h3>Chat requests</h3>
            <ul className="list">
              {data.requests.map((request, index) => (
                <li key={index}>
                  {request.sender === "reporter" ? "Reporter" : "Reported"} sent one · {request.status} ·{" "}
                  {when(request.created_at)}
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
      {mediaNotice && (
        <p className="notice" role="status">
          {mediaNotice}
        </p>
      )}

      {open && (
        <ActionPanel
          launchData={launchData}
          path={`/api/moderation/reports/${data.id}/action`}
          kinds={["close", "warn", "suspend", "ban"]}
          closesReport
          nickname={data.reported.nickname ?? "this account"}
          onDone={onDone}
          onFailure={onFailure}
        />
      )}
    </article>
  );
}
