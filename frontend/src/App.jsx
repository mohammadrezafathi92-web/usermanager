import React from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { useAuth } from "./context/AuthContext.jsx";
import { useLanguage } from "./context/LanguageContext.jsx";
import Login from "./pages/Login.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Users from "./pages/Users.jsx";
import UserDetail from "./pages/UserDetail.jsx";
import Nodes from "./pages/Nodes.jsx";
import Packages from "./pages/Packages.jsx";
import Tutorials from "./pages/Tutorials.jsx";
import Settings from "./pages/Settings.jsx";
import Admins from "./pages/Admins.jsx";
import RadiusLogs from "./pages/RadiusLogs.jsx";
import DiscountCodes from "./pages/DiscountCodes.jsx";

function Protected({ children }) {
  const { token, loading } = useAuth();
  const { t } = useLanguage();
  if (loading) {
    return <div className="min-h-screen flex items-center justify-center text-gray-400">{t("common.loading")}</div>;
  }
  if (!token) return <Navigate to="/login" replace />;
  return children;
}

// Gates /admins: superadmins manage level-2 Admins, level-2 Admins manage
// their OWN level-3 Sellers through the same page (see routers/admins.py's
// require_admin_or_above) - only a level-3 Seller is bounced to "/".
function AdminOrAboveOnly({ children }) {
  const { token, loading, isAdminOrAbove } = useAuth();
  const { t } = useLanguage();
  if (loading) {
    return <div className="min-h-screen flex items-center justify-center text-gray-400">{t("common.loading")}</div>;
  }
  if (!token) return <Navigate to="/login" replace />;
  if (!isAdminOrAbove) return <Navigate to="/" replace />;
  return children;
}

function PermRoute({ perm, children }) {
  // `perm` may be a single permission string or an array - an array means
  // "any one of these is enough" (used by pages made of several
  // independently-toggleable sub-permissions, e.g. /settings - see
  // permissions.py's PERMISSION_GROUPS.settings and task #230).
  const { token, loading, canAny } = useAuth();
  const { t } = useLanguage();
  if (loading) {
    return <div className="min-h-screen flex items-center justify-center text-gray-400">{t("common.loading")}</div>;
  }
  if (!token) return <Navigate to="/login" replace />;
  if (!canAny(Array.isArray(perm) ? perm : [perm])) return <Navigate to="/" replace />;
  return children;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        path="/"
        element={
          <Protected>
            <Dashboard />
          </Protected>
        }
      />
      <Route
        path="/users"
        element={
          <Protected>
            <Users />
          </Protected>
        }
      />
      <Route
        path="/users/:id"
        element={
          <Protected>
            <UserDetail />
          </Protected>
        }
      />
      <Route
        path="/nodes"
        element={
          <PermRoute perm={["view_nodes", "edit_nodes", "delete_nodes"]}>
            <Nodes />
          </PermRoute>
        }
      />
      <Route
        path="/packages"
        element={
          <PermRoute perm={["view_packages", "edit_packages", "delete_packages"]}>
            <Packages />
          </PermRoute>
        }
      />
      <Route
        path="/tutorials"
        element={
          <PermRoute perm={["view_tutorials", "edit_tutorials", "delete_tutorials"]}>
            <Tutorials />
          </PermRoute>
        }
      />
      <Route
        path="/settings"
        element={
          <PermRoute
            perm={["manage_payment_settings", "manage_bot_settings", "manage_api_keys", "manage_backup", "manage_discount_codes"]}
          >
            <Settings />
          </PermRoute>
        }
      />
      <Route
        path="/admins"
        element={
          <AdminOrAboveOnly>
            <Admins />
          </AdminOrAboveOnly>
        }
      />
      <Route
        path="/radius-logs"
        element={
          <Protected>
            <RadiusLogs />
          </Protected>
        }
      />
      <Route
        path="/discount-codes"
        element={
          <PermRoute perm="manage_discount_codes">
            <DiscountCodes />
          </PermRoute>
        }
      />
      <Route path="/a/:slug" element={<Navigate to="/login" replace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
