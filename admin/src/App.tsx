import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import type { ReactNode } from "react";

import { useSession } from "./api/session";
import { AftersalesPage } from "./pages/AftersalesPage";
import { AuditPage } from "./pages/AuditPage";
import { FundsPage } from "./pages/FundsPage";
import { LoginPage } from "./pages/LoginPage";
import { OrderDetailPage } from "./pages/OrderDetailPage";
import { OrdersPage } from "./pages/OrdersPage";
import { OverviewPage } from "./pages/OverviewPage";
import { SettingsPage } from "./pages/SettingsPage";
import { UsersPage } from "./pages/UsersPage";
import { WorkersPage } from "./pages/WorkersPage";

const NAVIGATION = [
  { to: "/overview", label: "总览" },
  { to: "/orders", label: "订单" },
  { to: "/aftersales", label: "售后" },
  { to: "/workers", label: "Worker" },
  { to: "/users", label: "用户" },
  { to: "/funds", label: "资金" },
  { to: "/settings", label: "设置" },
  { to: "/audit", label: "审计" },
] as const;

/** Sends anonymous visitors to /login, remembering where they were headed. */
function RequireAdmin({ children }: { children: ReactNode }) {
  const { status } = useSession();
  const location = useLocation();

  if (status === "loading") {
    return <p role="status">正在确认登录状态…</p>;
  }
  if (status === "anonymous") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}

function Shell({ children }: { children: ReactNode }) {
  const { identity, signOut } = useSession();

  return (
    <div className="shell">
      <header className="shell__header">
        <span className="shell__brand">竞赛批改 · 管理台</span>
        <nav className="shell__nav">
          {NAVIGATION.map((item) => (
            <NavLink key={item.to} to={item.to} className="shell__link">
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="shell__account">
          <span>{identity?.username}</span>
          <button type="button" onClick={() => void signOut()}>
            退出
          </button>
        </div>
      </header>
      <main className="shell__main">{children}</main>
    </div>
  );
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/*"
        element={
          <RequireAdmin>
            <Shell>
              <Routes>
                <Route path="/" element={<Navigate to="/overview" replace />} />
                <Route path="/overview" element={<OverviewPage />} />
                <Route path="/orders" element={<OrdersPage />} />
                <Route path="/orders/:orderId" element={<OrderDetailPage />} />
                <Route path="/aftersales" element={<AftersalesPage />} />
                <Route path="/workers" element={<WorkersPage />} />
                <Route path="/users" element={<UsersPage />} />
                <Route path="/funds" element={<FundsPage />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="/audit" element={<AuditPage />} />
              </Routes>
            </Shell>
          </RequireAdmin>
        }
      />
    </Routes>
  );
}
