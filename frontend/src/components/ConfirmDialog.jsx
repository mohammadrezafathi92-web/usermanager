import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { useLanguage } from "../context/LanguageContext.jsx";

/**
 * Panel-wide replacement for window.confirm(). Mounted once at the app
 * root; any component calls useConfirm() to get an async confirm(options)
 * function instead of the native (unstyled, non-dark-mode, non-RTL) dialog.
 *
 * Visual/interaction conventions copied from Modal.jsx: bottom-sheet on
 * mobile / centered dialog on sm:+, same backdrop, Escape-to-dismiss and
 * body-scroll-lock keyed only on whether a dialog is open (not on the
 * resolver identity) so the effect doesn't tear down/re-run every render.
 */
const ConfirmContext = createContext(null);

export function ConfirmProvider({ children }) {
  const { t } = useLanguage();
  const [state, setState] = useState(null); // { title, message, confirmText, cancelText, danger, resolve }
  const resolveRef = useRef(null);

  const confirm = useCallback((options) => {
    const opts = typeof options === "string" ? { message: options } : options || {};
    return new Promise((resolve) => {
      resolveRef.current = resolve;
      setState(opts);
    });
  }, []);

  const settle = useCallback((result) => {
    if (resolveRef.current) resolveRef.current(result);
    resolveRef.current = null;
    setState(null);
  }, []);

  const open = !!state;

  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (e) => {
      if (e.key === "Escape") settle(false);
    };
    document.addEventListener("keydown", onKeyDown);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = prevOverflow;
    };
  }, [open, settle]);

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {open && (
        <div className="fixed inset-0 z-[100] flex items-end sm:items-center justify-center sm:p-4">
          <div
            className="absolute inset-0 bg-gray-900/50 backdrop-blur-[1px]"
            onClick={() => settle(false)}
          />
          <div
            role="dialog"
            aria-modal="true"
            aria-label={state.title || t("confirmDialog.title")}
            className="relative bg-white dark:bg-slate-900 shadow-xl w-full max-w-sm rounded-t-2xl sm:rounded-2xl p-5 space-y-4"
          >
            <div className="flex items-center gap-2">
              <div
                className={`w-9 h-9 shrink-0 rounded-xl flex items-center justify-center ${
                  state.danger
                    ? "bg-red-50 text-red-600 dark:bg-red-500/10"
                    : "bg-amber-50 text-amber-600 dark:bg-amber-500/10"
                }`}
              >
                <AlertTriangle size={18} />
              </div>
              <div className="font-bold text-gray-800 dark:text-gray-100">
                {state.title || t("confirmDialog.title")}
              </div>
            </div>

            {state.message && (
              <div className="text-sm text-gray-600 dark:text-gray-300 whitespace-pre-line">
                {state.message}
              </div>
            )}

            <div className="flex gap-2">
              <button type="button" className="btn-secondary flex-1" onClick={() => settle(false)}>
                {state.cancelText || t("common.cancel")}
              </button>
              <button
                type="button"
                autoFocus
                className={`flex-1 ${state.danger ? "btn-danger" : "btn-primary"}`}
                onClick={() => settle(true)}
              >
                {state.confirmText || t("common.confirm")}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  );
}

/** Returns an async confirm(messageOrOptions) -> Promise<boolean>. */
export function useConfirm() {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error("useConfirm must be used within ConfirmProvider");
  return ctx;
}
