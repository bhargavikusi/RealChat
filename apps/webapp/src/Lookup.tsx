import { useEffect, useRef, useState } from "react";

import { ApiError, getJson, type Failure, type Match } from "./api";

interface Props {
  launchData: string;
  onOpen: (accountId: number) => void;
  onFailure: (failure: Failure) => void;
}

/** Find an account by its RealChat ID. */
export function Lookup({ launchData, onOpen, onFailure }: Props) {
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<Match[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  // A search that answers after leaving this screen must not open an account.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  async function search() {
    const value = query.trim();
    if (!value) return;
    setBusy(true);
    setMessage(null);
    setMatches(null);
    try {
      const found = await getJson<Match[]>(
        `/api/moderation/accounts?query=${encodeURIComponent(value)}`,
        launchData,
      );
      if (!mounted.current) return;
      setMatches(found);
      if (found.length === 1 && found[0]) onOpen(found[0].id);
    } catch (error) {
      if (!mounted.current) return;
      const failure = error instanceof ApiError ? error.failure : "error";
      if (failure === "reopen" || failure === "denied") onFailure(failure);
      else setMessage("The search couldn't be done. Please try again.");
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  return (
    <section aria-label="Account lookup">
      <h2>Find an account</h2>
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          void search();
        }}
      >
        <label>
          RealChat ID
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            maxLength={64}
            autoComplete="off"
            inputMode="text"
          />
        </label>
        <p className="hint">As shown on profile cards, e.g. /userid_k7m3qxa.</p>
        <button type="submit" className="primary" disabled={busy || !query.trim()}>
          Search
        </button>
      </form>
      {message && (
        <p className="notice" role="alert">
          {message}
        </p>
      )}
      {matches?.length === 0 && <p className="hint">No account found.</p>}
      {matches && matches.length > 1 && (
        <ul className="list">
          {matches.map((match) => (
            <li key={match.id}>
              <button type="button" className="row" onClick={() => onOpen(match.id)}>
                <span className="row-title">{match.nickname ?? "Deleted account"}</span>
                <span className="row-meta">
                  {match.public_id ? `ID ${match.public_id} · ` : ""}
                  {match.status}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
