import { useCallback, useEffect, useState } from "react";

import { AccountView } from "./AccountView";
import { ApiError, getJson, type Failure, type Me } from "./api";
import { Lookup } from "./Lookup";
import { Queue } from "./Queue";
import { ReportView } from "./ReportView";
import { webApp } from "./telegram";

type State = { kind: "loading" } | { kind: "signedIn"; me: Me } | { kind: Failure };
type Screen =
  | { name: "queue" }
  | { name: "report"; id: number; back: Screen }
  | { name: "lookup" }
  | { name: "account"; id: number };

const ROLE_LABELS: Record<Me["role"], string> = { moderator: "Moderator", admin: "Admin" };

const MESSAGES: Record<Failure, string> = {
  reopen: "Please open this again from 🛡 Moderation in RealChat's Advanced settings.",
  denied: "This panel is for RealChat moderators and admins.",
  conflict: "Something changed meanwhile. Please try again.",
  notFound: "That wasn't found.",
  invalid: "That request wasn't valid.",
  telegram: "Telegram couldn't be reached. Please try again.",
  error: "Something went wrong. Please try again.",
};

/** Where Telegram's back button goes from a screen, if anywhere. */
function backFrom(screen: Screen): Screen | null {
  if (screen.name === "report") return screen.back;
  if (screen.name === "account") return { name: "lookup" };
  return null;
}

export function App() {
  const launchData = webApp()?.initData ?? "";
  const [state, setState] = useState<State>(launchData ? { kind: "loading" } : { kind: "reopen" });
  const [screen, setScreen] = useState<Screen>({ name: "queue" });
  const [notice, setNotice] = useState<string | null>(null);

  // Sign-in problems end the session; anything else returns to the reports with a
  // notice. Failures within a screen are shown there, with a way to try again.
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

  useEffect(() => {
    const back = webApp()?.BackButton;
    if (!back) return;
    const target = backFrom(screen);
    const go = () => {
      if (target) setScreen(target);
    };
    if (target) {
      back.show();
      back.onClick(go);
    } else {
      back.hide();
    }
    return () => back.offClick(go);
  }, [screen]);

  const tab = screen.name === "lookup" || screen.name === "account" || (screen.name === "report" && screen.back.name === "account") ? "lookup" : "reports";

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
      {state.kind === "signedIn" && (
        <>
          <nav className="tabs" aria-label="Sections">
            <button
              type="button"
              aria-current={tab === "reports" ? "page" : undefined}
              onClick={() => {
                setNotice(null);
                setScreen({ name: "queue" });
              }}
            >
              Reports
            </button>
            <button
              type="button"
              aria-current={tab === "lookup" ? "page" : undefined}
              onClick={() => {
                setNotice(null);
                setScreen({ name: "lookup" });
              }}
            >
              Lookup
            </button>
          </nav>
          {screen.name === "queue" && (
            <Queue
              launchData={launchData}
              notice={notice}
              onOpen={(id) => {
                setNotice(null);
                setScreen({ name: "report", id, back: { name: "queue" } });
              }}
              onFailure={fail}
            />
          )}
          {screen.name === "report" && (
            <ReportView
              launchData={launchData}
              reportId={screen.id}
              onDone={(done) => {
                setNotice(done);
                setScreen(screen.back.name === "account" ? screen.back : { name: "queue" });
              }}
              onFailure={fail}
            />
          )}
          {screen.name === "lookup" && <Lookup launchData={launchData} onOpen={(id) => setScreen({ name: "account", id })} onFailure={fail} />}
          {screen.name === "account" && (
            <AccountView
              key={screen.id}
              launchData={launchData}
              accountId={screen.id}
              isAdmin={state.me.role === "admin"}
              onOpenReport={(id) => setScreen({ name: "report", id, back: screen })}
              onFailure={fail}
            />
          )}
        </>
      )}
    </main>
  );
}
