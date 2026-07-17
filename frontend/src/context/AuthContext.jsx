import React, { createContext, useContext, useEffect, useState } from "react";
import { login as apiLogin, fetchMe } from "../api/client.js";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [token, setToken] = useState(() => localStorage.getItem("um_token"));
  const [adminId, setAdminId] = useState(null);
  const [username, setUsername] = useState(null);
  const [isSuperadmin, setIsSuperadmin] = useState(false);
  // 3-tier hierarchy role ("superadmin" | "admin" | "seller" - see backend
  // services/hierarchy.py). A level-2 "admin" gets the exact same full
  // menu access as a superadmin (just scoped to their own tree server-side) -
  // only a level-3 "seller" is ever actually gated by `permissions` below.
  const [role, setRole] = useState("seller");
  const [permissions, setPermissions] = useState([]);
  const [loading, setLoading] = useState(true);

  const applyMe = (data) => {
    setAdminId(data.id ?? null);
    setUsername(data.username);
    setIsSuperadmin(!!data.is_superadmin);
    setRole(data.role || (data.is_superadmin ? "superadmin" : "seller"));
    setPermissions(data.permissions || []);
  };

  useEffect(() => {
    if (!token) {
      setLoading(false);
      return;
    }
    fetchMe()
      .then((res) => applyMe(res.data))
      .catch(() => {
        localStorage.removeItem("um_token");
        setToken(null);
      })
      .finally(() => setLoading(false));
  }, [token]);

  const login = async (u, p) => {
    const res = await apiLogin(u, p);
    localStorage.setItem("um_token", res.data.access_token);
    setToken(res.data.access_token);
    const me = await fetchMe();
    applyMe(me.data);
  };

  const logout = () => {
    localStorage.removeItem("um_token");
    setToken(null);
    setAdminId(null);
    setUsername(null);
    setIsSuperadmin(false);
    setRole("seller");
    setPermissions([]);
  };

  // A level-2 Admin gets the same unconditional "yes" a superadmin does -
  // see backend deps.py's require_permission docstring for why (full panel
  // access within their own tree is the whole point of this tier).
  const isAdminOrAbove = isSuperadmin || role === "admin";

  // true if this admin can see/use a given panel section - superadmins and
  // level-2 Admins can always do everything; a section not in
  // PERMISSION_CHOICES (i.e. user management/dashboard) is available to
  // every logged-in admin regardless of tier.
  const can = (perm) => isAdminOrAbove || permissions.includes(perm);

  // true if this admin has AT LEAST ONE of the given permissions - used for
  // pages/routes made of several independently-toggleable sub-permissions
  // (e.g. /settings, whose tabs are each gated by their own permission -
  // see task #230/permissions.py's PERMISSION_GROUPS.settings).
  const canAny = (perms) => isAdminOrAbove || (perms || []).some((p) => permissions.includes(p));

  return (
    <AuthContext.Provider
      value={{ token, adminId, username, isSuperadmin, role, isAdminOrAbove, permissions, can, canAny, loading, login, logout }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
