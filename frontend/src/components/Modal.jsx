import React, { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

/**
 * Used by nearly every page for create/edit/confirm dialogs.
 *
 * On phones it renders as a bottom sheet (full width, anchored to the bottom,
 * rounded top corners) rather than a centred box: panel forms are long, and a
 * centred dialog on a short viewport put the submit button below the fold
 * with the on-screen keyboard open. From `sm:` up it is the usual centred
 * dialog. The header is sticky so the title and close button stay reachable
 * while scrolling a long form.
 *
 * Accessibility: rendered into document.body with the app root made `inert`
 * (and aria-hidden) while open, so neither the keyboard nor a screen reader
 * can reach the page behind it; Tab / Shift+Tab cycle inside the dialog; the
 * first element marked `data-autofocus` (else the first focusable control)
 * gets the focus on open; on close the focus returns to whatever opened it.
 * `dismissible={false}` (e.g. while a destructive request is running)
 * disables Escape, the backdrop and the close button.
 */
const FOCUSABLE = 'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), ' +
  'select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export default function Modal({ open, onClose, title, children, width = "max-w-lg", dismissible = true }) {
  // Every call site passes an inline arrow (`onClose={() => setOpen(false)}`),
  // so `onClose` is a different function on each render of the owning page.
  // Holding the latest callback in a ref keeps the Effects keyed purely on
  // `open`, which is what actually changes.
  const onCloseRef = useRef(onClose);
  const dismissibleRef = useRef(dismissible);
  const panelRef = useRef(null);
  useEffect(() => {
    onCloseRef.current = onClose;
    dismissibleRef.current = dismissible;
  });

  useEffect(() => {
    if (!open) return undefined;
    const opener = document.activeElement;
    const root = document.getElementById("root");
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    if (root) {
      root.setAttribute("inert", "");
      root.setAttribute("aria-hidden", "true");
    }
    const focusFirst = () => {
      const panel = panelRef.current;
      if (!panel || panel.contains(document.activeElement)) return;
      const target = panel.querySelector("[data-autofocus]") || panel.querySelector(FOCUSABLE) || panel;
      target.focus();
    };
    focusFirst();
    const onKey = (e) => {
      // Only the topmost dialog answers (a password prompt over this one).
      const dialogs = document.querySelectorAll('[role="dialog"][aria-modal="true"]');
      if (dialogs[dialogs.length - 1] !== panelRef.current) return;
      if (e.key === "Escape") {
        if (dismissibleRef.current) onCloseRef.current?.();
        return;
      }
      if (e.key !== "Tab" || !panelRef.current) return;
      const items = Array.from(panelRef.current.querySelectorAll(FOCUSABLE)).filter((el) => !el.hasAttribute("inert"));
      if (!items.length) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && (document.activeElement === first || !panelRef.current.contains(document.activeElement))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !panelRef.current.contains(document.activeElement))) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
      if (root) {
        root.removeAttribute("inert");
        root.removeAttribute("aria-hidden");
      }
      if (opener && typeof opener.focus === "function" && document.contains(opener)) opener.focus();
    };
  }, [open]);

  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center sm:p-4">
      <div className="absolute inset-0 bg-gray-900/50 backdrop-blur-[1px]" onClick={() => { if (dismissible) onClose?.(); }} />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        tabIndex={-1}
        className={`relative bg-white dark:bg-slate-900 shadow-xl w-full ${width}
                    rounded-t-2xl sm:rounded-2xl max-h-[92vh] sm:max-h-[90vh] overflow-y-auto overscroll-contain`}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between gap-3 px-5 py-4 bg-white border-b border-gray-100 dark:bg-slate-900 dark:border-slate-800">
          <h3 className="font-bold text-gray-800 min-w-0 truncate">{title}</h3>
          <button
            type="button"
            onClick={onClose}
            disabled={!dismissible}
            className="btn-ghost btn-icon shrink-0 text-gray-400"
            aria-label="بستن"
          >
            <X size={20} />
          </button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
