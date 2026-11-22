import { useEffect, useRef, useState } from "react";

import { ApiError, postJson, type ActionBody, type ActionKind, type Failure } from "./api";
import { REFUSALS, RULES, SUSPENSION_DAYS } from "./labels";
import { confirmAction } from "./telegram";

interface Props {
  launchData: string;
  reportId: number;
  nickname: string;
  onDone: (notice: string) => void;
  onFailure: (failure: Failure) => void;
}

const BUTTONS: { kind: ActionKind; label: string }[] = [
  { kind: "close", label: "Close, no action" },
  { kind: "warn", label: "Warn" },
  { kind: "suspend", label: "Suspend" },
  { kind: "ban", label: "Ban" },
];

const DONE: Record<ActionKind, string> = {
  close: "Report closed with no action.",
  warn: "Warning recorded; they'll be notified. The report is closed.",
  suspend: "Account suspended. The report is closed.",
  ban: "Account banned. The report is closed.",
};

function question(kind: ActionKind, nickname: string, rule: string, days: number): string {
  const name = RULES[rule] ?? rule;
  if (kind === "close") return "Close this report with no action?";
  if (kind === "warn") return `Warn ${nickname} for: ${name}?`;
  if (kind === "suspend") return `Suspend ${nickname} for ${days} day${days === 1 ? "" : "s"} for: ${name}?`;
  return `Ban ${nickname} for: ${name}? Only an admin can lift a ban.`;
}

/** The actions on a report, each confirmed in Telegram before it is sent. */
export function ActionPanel({ launchData, reportId, nickname, onDone, onFailure }: Props) {
  const [kind, setKind] = useState<ActionKind | null>(null);
  const [rule, setRule] = useState("");
  const [days, setDays] = useState<number>(SUSPENSION_DAYS[0]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  // An answer that arrives after leaving this report must not change another screen.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  async function submit(chosen: ActionKind) {
    if (chosen !== "close" && !rule) {
      setMessage("Choose the rule that was broken.");
      return;
    }
    if (!(await confirmAction(question(chosen, nickname, rule, days)))) return;
    const body: ActionBody =
      chosen === "close" ? { kind: chosen } : chosen === "suspend" ? { kind: chosen, rule, days } : { kind: chosen, rule };
    setBusy(true);
    setMessage(null);
    try {
      await postJson(`/api/moderation/reports/${reportId}/action`, launchData, body);
      if (mounted.current) onDone(DONE[chosen]);
    } catch (error) {
      if (!mounted.current) return;
      if (error instanceof ApiError && error.failure === "conflict") {
        setMessage(REFUSALS[error.code ?? ""] ?? "This action no longer applies.");
      } else if (error instanceof ApiError && (error.failure === "reopen" || error.failure === "denied")) {
        onFailure(error.failure);
      } else {
        setMessage("Something went wrong. Please try again.");
      }
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  return (
    <section className="actions" aria-label="Actions">
      <h3>Decision</h3>
      <div className="buttons">
        {BUTTONS.map((button) => (
          <button
            key={button.kind}
            type="button"
            className={button.kind === "ban" ? "danger" : undefined}
            aria-pressed={kind === button.kind}
            disabled={busy}
            onClick={() => {
              setMessage(null);
              if (button.kind === "close") {
                setKind(null);
                void submit("close");
              } else {
                setKind(button.kind);
              }
            }}
          >
            {button.label}
          </button>
        ))}
      </div>
      {kind !== null && (
        <form
          className="form"
          onSubmit={(event) => {
            event.preventDefault();
            void submit(kind);
          }}
        >
          <label>
            Rule broken
            <select
              value={rule}
              onChange={(event) => {
                setRule(event.target.value);
                setMessage(null);
              }}
            >
              <option value="">Choose…</option>
              {Object.entries(RULES).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          {kind === "suspend" && (
            <label>
              Days
              <select value={days} onChange={(event) => setDays(Number(event.target.value))}>
                {SUSPENSION_DAYS.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button type="submit" className={kind === "ban" ? "danger" : "primary"} disabled={busy}>
            {kind === "warn" ? "Send warning" : kind === "suspend" ? "Suspend" : "Ban"}
          </button>
        </form>
      )}
      {message && (
        <p className="notice" role="alert">
          {message}
        </p>
      )}
    </section>
  );
}
