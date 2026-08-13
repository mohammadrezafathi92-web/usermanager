import React, { useEffect, useRef } from "react";
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
 */
export default function Modal({ open, onClose, title, children, width = "max-w-lg" }) {
  // Every call site passes an inline arrow (`onClose={() => setOpen(false)}`),
  // so `onClose` is a different function on each render of the owning page.
  // Listing it as a dependency below would tear down and re-run this Effect
  // continuously - re-reading `document.body.style.overflow` each time, so the
  // "previous" value it restores on close would be the "hidden" it set itself,
  // leaving the page unscrollable. Holding the latest callback in a ref keeps
  // the Effect keyed purely on `open`, which is what actually changes.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  // Escape to dismiss + lock the background scroll while open. Neither
  // existed before, so on mobile the page behind the dialog scrolled under
  // your finger whenever the dialog's own content had nothing left to scroll.
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") onCloseRef.current?.();
    };
    document.addEventListener("keydown", onKey);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center sm:p-4">
      <div className="absolute inset-0 bg-gray-900/50 backdrop-blur-[1px]" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        className={`relative bg-white dark:bg-slate-900 shadow-xl w-full ${width}
                    rounded-t-2xl sm:rounded-2xl max-h-[92vh] sm:max-h-[90vh] overflow-y-auto`}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between gap-3 px-5 py-4 bg-white border-b border-gray-100 dark:bg-slate-900 dark:border-slate-800">
          <h3 className="font-bold text-gray-800 min-w-0 truncate">{title}</h3>
          <button
            type="button"
            onClick={onClose}
            className="btn-ghost btn-icon shrink-0 text-gray-400"
            aria-label="بستن"
          >
            <X size={20} />
          </button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}
