import React, { createContext, useContext, useEffect, useState } from "react";
import { login as apiLogin, fetchMe } from "../api/client.js";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [token, setToken] = useState(() => localStorage.getItem("um_token"));
  const [username, setUsername] = useState(null);
  const [isSuperadmin, setIsSuperadmin] = useState(false);
  const [permissions, setPermissions] = useState([]);
  const [loading, setLoading] = useState(true);

  const applyMe = (data) => {
    setUsername(data.username);
    setIsSuperadmin(!!data.is_superadmin);
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
    setUsername(null);
    setIsSuperadmin(false);
    setPermissions([]);
  };

  // true if this admin can see/use a given panel section - superadmins can
  // always do everything; a section not in PERMISSION_CHOICES (i.e. user
  // management/dashboard) is available to every logged-in admin.
  const can = (perm) => isSuperadmin || permissions.includes(perm);

  return (
    <AuthContext.Provider value={{ token, username, isSuperadmin, permissions, can, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
