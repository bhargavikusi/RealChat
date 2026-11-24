import { lazy, Suspense, useCallback, useEffect, useState } from "react";

import { AccountView } from "./AccountView";
import { DEFAULT_ANALYTICS, type AnalyticsChoice } from "./analytics";
import { ApiError, getJson, type Failure, type Me } from "./api";
import { Lookup } from "./Lookup";
import { Queue } from "./Queue";
import { ReportView } from "./ReportView";
import { DEFAULT_FILTERS, UsersView, type UserFilters } from "./UsersView";
import { webApp } from "./telegram";

// Loaded only when an admin opens Analytics: the charts are most of the app's size.
const AnalyticsView = lazy(() => import("./AnalyticsView").then((module) => ({ default: module.AnalyticsView })));

type State = { kind: "loading" } | { kind: "signedIn"; me: Me } | { kind: Failure };
type Screen =
  | { name: "queue" }
  | { name: "report"; id: number; back: Screen }
  | { name: "lookup" }
  | { name: "account"; id: number; back: Screen }
  | { name: "users" }
  | { name: "analytics" };

type Tab = "reports" | "lookup" | "users" | "analytics";

/** The tab a screen belongs to: a report or account belongs to where it was opened. */
function tabOf(screen: Screen): Tab {
  if (screen.name === "report" || screen.name === "account") return tabOf(screen.back);
  if (screen.name === "lookup") return "lookup";
  if (screen.name === "users") return "users";
  if (screen.name === "analytics") return "analytics";
  return "reports";
}

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

/** The first screen: the report in `?report=<id>` (an urgent-report alert's button),
 * else the reports queue. */
function startScreen(): Screen {
  const id = Number(new URLSearchParams(window.location.search).get("report"));
  return Number.isInteger(id) && id > 0 ? { name: "report", id, back: { name: "queue" } } : { name: "queue" };
}

/** Where Telegram's back button goes from a screen, if anywhere. */
function backFrom(screen: Screen): Screen | null {
  if (screen.name === "report") return screen.back;
  if (screen.name === "account") return screen.back;
  return null;
}

export function App() {
  const launchData = webApp()?.initData ?? "";
  const [state, setState] = useState<State>(launchData ? { kind: "loading" } : { kind: "reopen" });
  const [screen, setScreen] = useState<Screen>(startScreen);
  const [notice, setNotice] = useState<string | null>(null);
  // Kept here, so going back from an account to the directory keeps the search.
  const [userFilters, setUserFilters] = useState<UserFilters>(DEFAULT_FILTERS);
  // And going back from an account to the analytics keeps the dashboard and period.
  const [analytics, setAnalytics] = useState<AnalyticsChoice>(DEFAULT_ANALYTICS);

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

  const tab = tabOf(screen);

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
            {state.me.role === "admin" && (
              <button
                type="button"
                aria-current={tab === "users" ? "page" : undefined}
                onClick={() => {
                  setNotice(null);
                  setScreen({ name: "users" });
                }}
              >
                Users
              </button>
            )}
            {state.me.role === "admin" && (
              <button
                type="button"
                aria-current={tab === "analytics" ? "page" : undefined}
                onClick={() => {
                  setNotice(null);
                  setScreen({ name: "analytics" });
                }}
              >
                Analytics
              </button>
            )}
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
          {screen.name === "lookup" && (
            <Lookup
              launchData={launchData}
              onOpen={(id) => {
                setNotice(null);
                setScreen({ name: "account", id, back: { name: "lookup" } });
              }}
              onFailure={fail}
            />
          )}
          {screen.name === "analytics" && state.me.role === "admin" && (
            <Suspense fallback={<p className="hint">Loading…</p>}>
              <AnalyticsView
                launchData={launchData}
                choice={analytics}
                onChoice={setAnalytics}
                onOpenAccount={(id) => {
                  setNotice(null);
                  setScreen({ name: "account", id, back: { name: "analytics" } });
                }}
                onFailure={fail}
              />
            </Suspense>
          )}
          {screen.name === "users" && state.me.role === "admin" && (
            <UsersView
              launchData={launchData}
              filters={userFilters}
              onFilters={setUserFilters}
              onOpen={(id) => {
                setNotice(null);
                setScreen({ name: "account", id, back: { name: "users" } });
              }}
              onFailure={fail}
            />
          )}
          {screen.name === "account" && (
            <AccountView
              key={screen.id}
              launchData={launchData}
              accountId={screen.id}
              isAdmin={state.me.role === "admin"}
              notice={notice}
              onOpenReport={(id) => {
                setNotice(null);
                setScreen({ name: "report", id, back: screen });
              }}
              onFailure={fail}
            />
          )}
        </>
      )}
    </main>
  );
}
