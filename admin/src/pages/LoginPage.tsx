import { useState } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { AdminApiError } from "../api/client";
import { useSession } from "../api/session";

/**
 * The password never leaves this component: it is posted once and not retained
 * in state after submission, and nothing is written to web storage.
 */
export function LoginPage() {
  const { status, signIn } = useSession();
  const location = useLocation() as { state?: { from?: string } };
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (status === "authenticated") {
    return <Navigate to={location.state?.from ?? "/overview"} replace />;
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await signIn(username, password);
    } catch (caught) {
      // Show the server's message verbatim. It is deliberately identical for an
      // unknown username and a wrong password, so the UI must not try to be
      // more specific than the server was.
      setError(
        caught instanceof AdminApiError ? caught.detail : "登录失败，请稍后再试。",
      );
    } finally {
      setPassword("");
      setSubmitting(false);
    }
  }

  return (
    <form className="login" onSubmit={submit}>
      <h1>管理台登录</h1>
      <label htmlFor="username">用户名</label>
      <input
        id="username"
        name="username"
        autoComplete="username"
        value={username}
        onChange={(event) => setUsername(event.target.value)}
        required
      />
      <label htmlFor="password">密码</label>
      <input
        id="password"
        name="password"
        type="password"
        autoComplete="current-password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        required
      />
      {error !== null && <p role="alert">{error}</p>}
      <button type="submit" disabled={submitting}>
        {submitting ? "登录中…" : "登录"}
      </button>
    </form>
  );
}
