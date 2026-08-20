import React, { createContext, useContext, useEffect, useState } from "react";
import { login as apiLogin, fetchMe } from "../api/client.js";
import { setDisplayOffset } from "../utils.js";

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
  // What is actually deployed - see backend services/version.py. Held
  // here rather than fetched separately because /me already carries it.
  const [build, setBuild] = useState({ version: null, commit: null });
  // This account's own wholesale credit. Needed by the Accounting page,
  // because giving credit to a Seller now deducts it from the giver - a
  // page offering that has to show what is left rather than let the first
  // sign of the limit be a refusal.
  const [wallet, setWallet] = useState({ balance: 0, credit_limit: 0, volume_balance_gb: 0 });
  const [loading, setLoading] = useState(true);

  const applyMe = (data) => {
    // Every date in the UI is rendered through utils.js's formatters, which
    // read this module-level offset. Setting it here means it is in place
    // before any page mounts, so nothing ever renders with the wrong clock
    // and then corrects itself.
    setDisplayOffset(data.display_utc_offset_minutes);
    setAdminId(data.id ?? null);
    setUsername(data.username);
    setIsSuperadmin(!!data.is_superadmin);
    setRole(data.role || (data.is_superadmin ? "superadmin" : "seller"));
    setPermissions(data.permissions || []);
    setBuild({ version: data.app_version || null, commit: data.app_commit || null });
    setWallet({
      balance: data.balance || 0,
      credit_limit: data.credit_limit || 0,
      volume_balance_gb: data.volume_balance_gb || 0,
    });
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
      value={{ build, wallet, token, adminId, username, isSuperadmin, role, isAdminOrAbove, permissions, can, canAny, loading, login, logout }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
