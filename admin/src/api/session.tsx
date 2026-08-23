import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";

import { AdminApiError, createAdminClient } from "./client";
import type { AdminClient } from "./client";

/**
 * Holds the signed-in admin and the CSRF token for the current session.
 *
 * The CSRF token lives in React state only — never in `localStorage` or
 * `sessionStorage`. It is re-fetched from `/auth/session` on mount, so a page
 * reload recovers it from the HttpOnly cookie rather than from anything the
 * page persisted. That means an injected script has nothing to read.
 */

export interface AdminIdentity {
  admin_id: string;
  username: string;
}

interface SessionState {
  status: "loading" | "authenticated" | "anonymous";
  identity: AdminIdentity | null;
  client: AdminClient;
  signIn: (username: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const SessionContext = createContext<SessionState | null>(null);

interface SessionResponse extends AdminIdentity {
  csrf_token: string;
}

export function SessionProvider({
  children,
  transport,
}: {
  children: ReactNode;
  /** Injected in tests so no request reaches the network. */
  transport?: typeof fetch;
}) {
  const [status, setStatus] = useState<SessionState["status"]>("loading");
  const [identity, setIdentity] = useState<AdminIdentity | null>(null);
  const [csrfToken, setCsrfToken] = useState<string | null>(null);

  const forget = useCallback(() => {
    setIdentity(null);
    setCsrfToken(null);
    setStatus("anonymous");
  }, []);

  const client = useMemo(
    () =>
      createAdminClient({
        transport,
        csrfToken: () => csrfToken,
        // A 401 from anywhere clears local UI state, so a stale screen can
        // never keep showing another admin's data after the session ends.
        onUnauthorized: forget,
      }),
    [transport, csrfToken, forget],
  );

  const refresh = useCallback(async () => {
    try {
      const session = await client.get<SessionResponse>("/auth/session");
      setIdentity({ admin_id: session.admin_id, username: session.username });
      setCsrfToken(session.csrf_token);
      setStatus("authenticated");
    } catch (error) {
      if (error instanceof AdminApiError && error.status === 401) {
        forget();
        return;
      }
      throw error;
    }
  }, [client, forget]);

  useEffect(() => {
    // Only probe once, on mount: the cookie is the source of truth, so this
    // recovers the session after a reload without persisting anything.
    if (status === "loading") void refresh();
  }, [status, refresh]);

  const signIn = useCallback(
    async (username: string, password: string) => {
      await client.post("/auth/login", { username, password });
      await refresh();
    },
    [client, refresh],
  );

  const signOut = useCallback(async () => {
    try {
      await client.post("/auth/logout", {});
    } catch {
      // Swallowed on purpose. The operator asked to leave, so the only correct
      // outcome is a signed-out UI; an already-expired session reports 401 here
      // and re-throwing would surface a spurious error for a successful intent.
    } finally {
      forget();
    }
  }, [client, forget]);

  const value = useMemo<SessionState>(
    () => ({ status, identity, client, signIn, signOut }),
    [status, identity, client, signIn, signOut],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  const value = useContext(SessionContext);
  if (value === null) {
    throw new Error("useSession must be used inside a SessionProvider");
  }
  return value;
}
