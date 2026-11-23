import { useEffect, useState } from "react";

import { ApiError, getJson, type Failure, type UserRow, type UsersPage } from "./api";
import { when } from "./labels";

/** The directory's search, filters, and sorting, as the form holds them (strings, "" for
 * no filter). They become the API's query parameters as they are. */
export interface UserFilters {
  query: string;
  vip: "" | "true" | "false";
  status: "" | "active" | "suspended" | "banned";
  role: "" | "user" | "moderator" | "admin";
  active_within_days: string;
  min_chats: string;
  joined_from: string;
  joined_to: string;
  country: string;
  gender: "" | "male" | "female" | "other";
  min_age: string;
  max_age: string;
  min_coins: string;
  max_coins: string;
  min_reports: string;
  sort: "newest" | "last_active" | "chats" | "coins" | "reports";
  descending: "true" | "false";
}

export const DEFAULT_FILTERS: UserFilters = {
  query: "",
  vip: "",
  status: "",
  role: "",
  active_within_days: "",
  min_chats: "",
  joined_from: "",
  joined_to: "",
  country: "",
  gender: "",
  min_age: "",
  max_age: "",
  min_coins: "",
  max_coins: "",
  min_reports: "",
  sort: "newest",
  descending: "true",
};

const SORTS: Record<UserFilters["sort"], string> = {
  newest: "Sign-up date",
  last_active: "Last active",
  chats: "Chats (30 days)",
  coins: "Coins",
  reports: "Reports received",
};

/** The query string for a page: only the filters that are set. */
export function usersQuery(filters: UserFilters, page: number): string {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(filters)) {
    const text = name === "country" ? value.trim().toUpperCase() : value.trim();
    if (text !== "") params.set(name, text);
  }
  params.set("page", String(page));
  return params.toString();
}

interface Props {
  launchData: string;
  filters: UserFilters;
  onFilters: (filters: UserFilters) => void;
  onOpen: (accountId: number) => void;
  onFailure: (failure: Failure) => void;
}

function Row({ row, onOpen }: { row: UserRow; onOpen: (id: number) => void }) {
  const about = [row.age, row.gender, row.country_code].filter(Boolean).join(" · ");
  const badges = [row.vip_until && "VIP", row.status !== "active" && row.status, row.role !== "user" && row.role].filter(Boolean);
  return (
    <li>
      <button type="button" className="row" onClick={() => onOpen(row.id)}>
        <span className="row-title">
          {row.nickname} <span className="hint">· ID {row.public_id}</span>
          {badges.map((badge) => (
            <span key={String(badge)} className="badge">
              {badge}
            </span>
          ))}
        </span>
        <span className="row-meta">
          {about && `${about} · `}
          {row.chats_30d} chats (30 days) · {row.coins} coins · {row.reports_received} reports
        </span>
        <span className="row-meta">
          Last active {when(row.last_active_at)} · joined {when(row.joined_at)}
        </span>
      </button>
    </li>
  );
}

/** Admins only: every account, with search, filters, sorting, and paging (D-072). */
export function UsersView({ launchData, filters, onFilters, onOpen, onFailure }: Props) {
  const [draft, setDraft] = useState<UserFilters>(filters);
  const [rows, setRows] = useState<UserRow[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [page, setPage] = useState(0);
  // Set where a load is started (here, Apply, Load more), and cleared when it ends.
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState<string | null>(null);
  // Bumped by Try again, to load the same page again after a failure.
  const [attempt, setAttempt] = useState(0);

  // Load a page whenever the applied filters or the page change.
  useEffect(() => {
    let active = true;
    getJson<UsersPage>(`/api/moderation/users?${usersQuery(filters, page)}`, launchData)
      .then((loaded) => {
        if (!active) return;
        setRows((current) => (page === 0 ? loaded.rows : [...current, ...loaded.rows]));
        setTotal(loaded.total);
      })
      .catch((error: unknown) => {
        if (!active) return;
        const failure = error instanceof ApiError ? error.failure : "error";
        if (failure === "reopen" || failure === "denied") onFailure(failure);
        else setFailed(failure === "invalid" ? "Check the filter values." : "The users couldn't be loaded.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [launchData, filters, page, onFailure, attempt]);

  function set<K extends keyof UserFilters>(name: K, value: UserFilters[K]) {
    setDraft((current) => ({ ...current, [name]: value }));
  }

  function apply(next: UserFilters) {
    setLoading(true);
    setFailed(null);
    setPage(0);
    setRows([]);
    setTotal(null); // no count for the old filters while the new ones load
    onFilters({ ...next }); // a new object, so the same filters load again too
  }

  const text = (name: keyof UserFilters, label: string, type = "text") => (
    <label>
      {label}
      <input type={type} value={draft[name]} onChange={(event) => set(name, event.target.value as never)} />
    </label>
  );

  return (
    <section aria-label="Users">
      <h2>Users</h2>
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          apply(draft);
        }}
      >
        <label>
          Search by RealChat ID or nickname
          <input value={draft.query} maxLength={64} onChange={(event) => set("query", event.target.value)} />
        </label>
        <details>
          <summary>Filters</summary>
          <label>
            VIP
            <select value={draft.vip} onChange={(event) => set("vip", event.target.value as UserFilters["vip"])}>
              <option value="">Any</option>
              <option value="true">VIP</option>
              <option value="false">Not VIP</option>
            </select>
          </label>
          <label>
            Status
            <select value={draft.status} onChange={(event) => set("status", event.target.value as UserFilters["status"])}>
              <option value="">Any</option>
              <option value="active">Active</option>
              <option value="suspended">Suspended</option>
              <option value="banned">Banned</option>
            </select>
          </label>
          <label>
            Role
            <select value={draft.role} onChange={(event) => set("role", event.target.value as UserFilters["role"])}>
              <option value="">Any</option>
              <option value="user">User</option>
              <option value="moderator">Moderator</option>
              <option value="admin">Admin</option>
            </select>
          </label>
          <label>
            Gender
            <select value={draft.gender} onChange={(event) => set("gender", event.target.value as UserFilters["gender"])}>
              <option value="">Any</option>
              <option value="male">Male</option>
              <option value="female">Female</option>
              <option value="other">Other</option>
            </select>
          </label>
          {text("active_within_days", "Active within (days)", "number")}
          {text("min_chats", "At least this many chats (30 days)", "number")}
          {text("joined_from", "Joined from", "date")}
          {text("joined_to", "Joined until", "date")}
          {text("country", "Country (2-letter code)")}
          {text("min_age", "Age from", "number")}
          {text("max_age", "Age to", "number")}
          {text("min_coins", "Coins from", "number")}
          {text("max_coins", "Coins to", "number")}
          {text("min_reports", "At least this many reports", "number")}
        </details>
        <label>
          Sort by
          <select value={draft.sort} onChange={(event) => set("sort", event.target.value as UserFilters["sort"])}>
            {Object.entries(SORTS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Order
          <select
            value={draft.descending}
            onChange={(event) => set("descending", event.target.value as UserFilters["descending"])}
          >
            <option value="true">Highest or newest first</option>
            <option value="false">Lowest or oldest first</option>
          </select>
        </label>
        <div className="buttons">
          <button type="submit" className="primary">
            Apply
          </button>
          <button
            type="button"
            onClick={() => {
              setDraft(DEFAULT_FILTERS);
              apply(DEFAULT_FILTERS);
            }}
          >
            Clear
          </button>
        </div>
      </form>
      {failed && (
        <div className="notice" role="alert">
          <p>{failed}</p>
          {/* The same page again: Load more never skips a page that failed. */}
          <button
            type="button"
            className="link"
            onClick={() => {
              setLoading(true);
              setFailed(null);
              setAttempt((count) => count + 1);
            }}
          >
            Try again
          </button>
        </div>
      )}
      {total !== null && (
        <p className="hint" role="status">
          {total} {total === 1 ? "user" : "users"}
        </p>
      )}
      <ul className="list">
        {rows.map((row) => (
          <Row key={row.id} row={row} onOpen={onOpen} />
        ))}
      </ul>
      {loading && <p className="hint">Loading…</p>}
      {!loading && !failed && total !== null && rows.length < total && (
        <button
          type="button"
          className="link"
          onClick={() => {
            setLoading(true);
            setFailed(null);
            setPage((current) => current + 1);
          }}
        >
          Load more
        </button>
      )}
    </section>
  );
}
