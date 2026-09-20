import React, { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import { CheckCircle2, Info, X, XCircle } from "lucide-react";
import { useLanguage } from "../context/LanguageContext.jsx";

/**
 * Panel-wide toast/notification system. Mounted once at the app root and
 * replaces native alert() (unstyled, blocking, no dark mode/RTL) plus the
 * ad-hoc timed setMsg() patterns scattered across pages.
 */
const ToastContext = createContext(null);

const ICONS = { success: CheckCircle2, error: XCircle, info: Info };
const TONES = {
  success: "border-emerald-500 text-emerald-600 dark:text-emerald-400",
  error: "border-red-500 text-red-600 dark:text-red-400",
  info: "border-sky-500 text-sky-600 dark:text-sky-400",
};

let seq = 0;

export function ToastProvider({ children }) {
  const { dir } = useLanguage();
  const [toasts, setToasts] = useState([]);
  const timers = useRef({});

  const dismiss = useCallback((id) => {
    setToasts((list) => list.filter((item) => item.id !== id));
    clearTimeout(timers.current[id]);
    delete timers.current[id];
  }, []);

  const push = useCallback(
    (message, type = "info", duration = 4000) => {
      const id = ++seq;
      setToasts((list) => [...list, { id, message, type }]);
      timers.current[id] = setTimeout(() => dismiss(id), duration);
      return id;
    },
    [dismiss]
  );

  const api = useMemo(
    () => ({
      show: push,
      success: (message, duration) => push(message, "success", duration),
      error: (message, duration) => push(message, "error", duration),
      info: (message, duration) => push(message, "info", duration),
    }),
    [push]
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        dir={dir}
        className="fixed z-[110] bottom-4 inset-x-4 sm:inset-x-auto sm:bottom-6 sm:end-6 flex flex-col gap-2 sm:w-80 pointer-events-none"
      >
        {toasts.map((item) => {
          const Icon = ICONS[item.type] || Info;
          return (
            <div
              key={item.id}
              role="status"
              className={`pointer-events-auto flex items-start gap-2 bg-white dark:bg-slate-900 shadow-xl rounded-xl border-s-4 ${
                TONES[item.type] || TONES.info
              } p-3 text-sm text-gray-800 dark:text-gray-100`}
            >
              <Icon size={18} className="shrink-0 mt-0.5" />
              <div className="flex-1 whitespace-pre-line">{item.message}</div>
              <button
                type="button"
                className="shrink-0 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
                onClick={() => dismiss(item.id)}
              >
                <X size={16} />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

/** Returns { show, success, error, info } to fire toasts from any component. */
export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}
