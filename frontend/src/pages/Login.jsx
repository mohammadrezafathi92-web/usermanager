import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Network, LogIn, Languages } from "lucide-react";
import { useAuth } from "../context/AuthContext.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";

export default function Login() {
  const { login } = useAuth();
  const { t, dir, toggleLanguage } = useLanguage();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login(username, password);
      navigate("/");
    } catch (err) {
      setError(err?.response?.data?.detail || t("login.error"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-brand-700 to-brand-900 p-4" dir={dir}>
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-sm p-8 relative">
        <button
          type="button"
          onClick={toggleLanguage}
          className="absolute top-4 left-4 text-xs text-gray-400 hover:text-gray-600 flex items-center gap-1"
        >
          <Languages size={14} /> {t("nav.language")}
        </button>
        <div className="flex flex-col items-center mb-6">
          <div className="w-14 h-14 rounded-2xl bg-brand-600 flex items-center justify-center text-white mb-3">
            <Network size={28} />
          </div>
          <h1 className="font-bold text-lg text-gray-800">{t("login.title")}</h1>
          <p className="text-sm text-gray-400 mt-1">{t("login.subtitle")}</p>
        </div>

        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("login.username")}</label>
            <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} required autoFocus />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("login.password")}</label>
            <input type="password" className="input" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </div>
          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
          <button type="submit" disabled={loading} className="btn-primary w-full">
            <LogIn size={16} />
            {loading ? t("login.submitting") : t("login.submit")}
          </button>
        </form>
      </div>
    </div>
  );
}
