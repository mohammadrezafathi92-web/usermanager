import React from "react";

/**
 * The panel's headline metric tile. `hint` is optional secondary text under
 * the label (e.g. "نسبت به ماه گذشته").
 *
 * Note `text-start`, not `text-right`: this component renders inside both the
 * Persian (RTL) and English (LTR) layouts, and a hardcoded right alignment
 * left the English panel with its numbers pushed to the wrong edge.
 */
export default function StatCard({ icon: Icon, label, value, tone = "brand", hint, onClick }) {
  const tones = {
    brand: "bg-gradient-to-br from-brand-400/20 to-brand-600/10 text-brand-600 ring-1 ring-inset ring-brand-500/15 dark:from-brand-400/25 dark:to-brand-600/10 dark:text-brand-400",
    emerald:
      "bg-gradient-to-br from-emerald-400/20 to-emerald-600/10 text-emerald-600 ring-1 ring-inset ring-emerald-500/15 dark:from-emerald-400/25 dark:to-emerald-600/10 dark:text-emerald-400",
    amber:
      "bg-gradient-to-br from-amber-400/20 to-amber-600/10 text-amber-600 ring-1 ring-inset ring-amber-500/15 dark:from-amber-400/25 dark:to-amber-600/10 dark:text-amber-400",
    red: "bg-gradient-to-br from-red-400/20 to-red-600/10 text-red-600 ring-1 ring-inset ring-red-500/15 dark:from-red-400/25 dark:to-red-600/10 dark:text-red-400",
  };
  const Comp = onClick ? "button" : "div";
  return (
    <Comp
      type={onClick ? "button" : undefined}
      onClick={onClick}
      className={`card flex items-center gap-3 sm:gap-4 w-full text-start ${
        onClick ? "cursor-pointer card-hover hover:-translate-y-0.5 transition" : ""
      }`}
    >
      <div className={`w-11 h-11 sm:w-12 sm:h-12 rounded-2xl flex items-center justify-center shrink-0 ${tones[tone]}`}>
        {/* Accepts either the icon COMPONENT (icon={Wallet}, the usual
            form) or an already-built element (icon={<Wallet size={18} />}).
            It used to accept only the first: passing an element made React
            try to call it as a component, which throws during render - and
            a throw here takes the ENTIRE page down to a blank white screen,
            not just this one tile. That is exactly what happened to the
            accounting section's "زیرمجموعه‌های من" and "طلب از نماینده‌ها"
            tabs (2026-09). Both call styles now work. */}
        {React.isValidElement(Icon) ? Icon : Icon ? <Icon size={22} /> : null}
      </div>
      <div className="min-w-0">
        <div className="text-xl sm:text-2xl font-bold font-mono text-gray-800 dark:text-gray-100 tnum truncate">{value}</div>
        <div className="text-sm text-gray-400 truncate">{label}</div>
        {hint && <div className="text-xs text-gray-400 mt-0.5 truncate">{hint}</div>}
      </div>
    </Comp>
  );
}
