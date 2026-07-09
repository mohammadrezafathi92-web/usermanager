import React from "react";
import { LogOut, User } from "lucide-react";
import { useAuth } from "../context/AuthContext.jsx";

export default function Topbar({ title, subtitle }) {
  const { username, logout } = useAuth();
  return (
    <div className="flex items-center justify-between mb-6">
      <div>
        <h1 className="text-xl font-bold text-gray-800">{title}</h1>
        {subtitle && <p className="text-sm text-gray-400 mt-1">{subtitle}</p>}
      </div>
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2 bg-white rounded-xl border border-gray-100 px-3 py-2 text-sm text-gray-600">
          <User size={16} />
          {username || "مدیر"}
        </div>
        <button onClick={logout} className="btn-secondary" title="خروج">
          <LogOut size={16} />
        </button>
      </div>
    </div>
  );
}
