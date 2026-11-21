import { useEffect, useState } from "react";

import { ApiError, getJson, type Failure, type Me } from "./api";
import { webApp } from "./telegram";

type State = { kind: "loading" } | { kind: "signedIn"; me: Me } | { kind: Failure };

const ROLE_LABELS: Record<Me["role"], string> = { moderator: "Moderator", admin: "Admin" };

const MESSAGES: Record<Failure, string> = {
  reopen: "Please open this from 🛡 Moderation in RealChat's Advanced settings.",
  denied: "This panel is for RealChat moderators and admins.",
  error: "Something went wrong. Please try again.",
};

export function App() {
  const launchData = webApp()?.initData ?? "";
  const [state, setState] = useState<State>(
    launchData ? { kind: "loading" } : { kind: "reopen" },
  );

  useEffect(() => {
    const app = webApp();
    app?.ready();
    app?.expand();
    if (!launchData) return;
    getJson<Me>("/api/moderation/me", launchData)
      .then((me) => setState({ kind: "signedIn", me }))
      .catch((error: unknown) =>
        setState({ kind: error instanceof ApiError ? error.failure : "error" }),
      );
  }, [launchData]);

  return (
    <main className="panel">
      <h1>🛡 Moderation</h1>
      {/* Announced to screen readers when it changes; failures as alerts. */}
      <div role="status" aria-live="polite">
        {state.kind === "loading" && <p className="hint">Signing in…</p>}
        {state.kind === "signedIn" && (
          <p>
            Signed in as <strong>{state.me.nickname ?? "you"}</strong> ·{" "}
            {ROLE_LABELS[state.me.role]}
          </p>
        )}
      </div>
      {state.kind !== "loading" && state.kind !== "signedIn" && (
        <p className="notice" role="alert">
          {MESSAGES[state.kind]}
        </p>
      )}
    </main>
  );
}
