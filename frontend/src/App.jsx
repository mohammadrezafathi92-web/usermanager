import React from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { useAuth } from "./context/AuthContext.jsx";
import Login from "./pages/Login.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Users from "./pages/Users.jsx";
import UserDetail from "./pages/UserDetail.jsx";
import Nodes from "./pages/Nodes.jsx";
import Packages from "./pages/Packages.jsx";
import Tutorials from "./pages/Tutorials.jsx";
import Settings from "./pages/Settings.jsx";
import Admins from "./pages/Admins.jsx";

function Protected({ children }) {
  const { token, loading } = useAuth();
  if (loading) {
    return <div className="min-h-screen flex items-center justify-center text-gray-400">در حال بارگذاری...</div>;
  }
  if (!token) return <Navigate to="/login" replace />;
  return children;
}

function SuperadminOnly({ children }) {
  const { token, loading, isSuperadmin } = useAuth();
  if (loading) {
    return <div className="min-h-screen flex items-center justify-center text-gray-400">در حال بارگذاری...</div>;
  }
  if (!token) return <Navigate to="/login" replace />;
  if (!isSuperadmin) return <Navigate to="/" replace />;
  return children;
}

function PermRoute({ perm, children }) {
  const { token, loading, can } = useAuth();
  if (loading) {
    return <div className="min-h-screen flex items-center justify-center text-gray-400">در حال بارگذاری...</div>;
  }
  if (!token) return <Navigate to="/login" replace />;
  if (!can(perm)) return <Navigate to="/" replace />;
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
          <PermRoute perm="manage_nodes">
            <Nodes />
          </PermRoute>
        }
      />
      <Route
        path="/packages"
        element={
          <PermRoute perm="manage_packages">
            <Packages />
          </PermRoute>
        }
      />
      <Route
        path="/tutorials"
        element={
          <PermRoute perm="manage_tutorials">
            <Tutorials />
          </PermRoute>
        }
      />
      <Route
        path="/settings"
        element={
          <PermRoute perm="manage_settings">
            <Settings />
          </PermRoute>
        }
      />
      <Route
        path="/admins"
        element={
          <SuperadminOnly>
            <Admins />
          </SuperadminOnly>
        }
      />
      <Route path="/a/:slug" element={<Navigate to="/login" replace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
