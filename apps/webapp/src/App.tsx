import { useCallback, useEffect, useState } from "react";

import { ApiError, getJson, type Failure, type Me } from "./api";
import { Queue } from "./Queue";
import { ReportView } from "./ReportView";
import { webApp } from "./telegram";

type State = { kind: "loading" } | { kind: "signedIn"; me: Me } | { kind: Failure };
type Screen = { name: "queue" } | { name: "report"; id: number };

const ROLE_LABELS: Record<Me["role"], string> = { moderator: "Moderator", admin: "Admin" };

const MESSAGES: Record<Failure, string> = {
  reopen: "Please open this again from 🛡 Moderation in RealChat's Advanced settings.",
  denied: "This panel is for RealChat moderators and admins.",
  conflict: "Something changed meanwhile. Please try again.",
  notFound: "That report wasn't found.",
  telegram: "Telegram couldn't be reached. Please try again.",
  error: "Something went wrong. Please try again.",
};

export function App() {
  const launchData = webApp()?.initData ?? "";
  const [state, setState] = useState<State>(launchData ? { kind: "loading" } : { kind: "reopen" });
  const [screen, setScreen] = useState<Screen>({ name: "queue" });
  const [notice, setNotice] = useState<string | null>(null);

  // Sign-in problems end the session; a missing report returns to the queue. Other
  // failures are shown where they happen, with a way to try again.
  const fail = useCallback((failure: Failure) => {
    if (failure === "reopen" || failure === "denied") {
      setState({ kind: failure });
    } else {
      setScreen({ name: "queue" });
      setNotice(MESSAGES[failure]);
    }
  }, []);

  useEffect(() => {
    const app = webApp();
    app?.ready();
    app?.expand();
    if (!launchData) return;
    getJson<Me>("/api/moderation/me", launchData)
      .then((me) => setState({ kind: "signedIn", me }))
      .catch((error: unknown) => setState({ kind: error instanceof ApiError ? error.failure : "error" }));
  }, [launchData]);

  // Telegram's back button returns from a report to the queue.
  useEffect(() => {
    const back = webApp()?.BackButton;
    if (!back) return;
    const toQueue = () => setScreen({ name: "queue" });
    if (screen.name === "report") {
      back.show();
      back.onClick(toQueue);
    } else {
      back.hide();
    }
    return () => back.offClick(toQueue);
  }, [screen]);

  return (
    <main className="panel">
      <header className="header">
        <h1>🛡 Moderation</h1>
        {/* Announced to screen readers when it changes; failures as alerts. */}
        <div role="status" aria-live="polite">
          {state.kind === "loading" && <p className="hint">Signing in…</p>}
          {state.kind === "signedIn" && (
            <p className="hint">
              {state.me.nickname ?? "You"} · {ROLE_LABELS[state.me.role]}
            </p>
          )}
        </div>
      </header>
      {state.kind !== "loading" && state.kind !== "signedIn" && (
        <p className="notice" role="alert">
          {MESSAGES[state.kind]}
        </p>
      )}
      {state.kind === "signedIn" && screen.name === "queue" && (
        <Queue
          launchData={launchData}
          notice={notice}
          onOpen={(id) => {
            setNotice(null);
            setScreen({ name: "report", id });
          }}
          onFailure={fail}
        />
      )}
      {state.kind === "signedIn" && screen.name === "report" && (
        <ReportView
          launchData={launchData}
          reportId={screen.id}
          onDone={(done) => {
            setNotice(done);
            setScreen({ name: "queue" });
          }}
          onFailure={fail}
        />
      )}
    </main>
  );
}
