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
    brand: "bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400",
    emerald: "bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400",
    amber: "bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400",
    red: "bg-red-50 text-red-600 dark:bg-red-500/10 dark:text-red-400",
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
        <Icon size={22} />
      </div>
      <div className="min-w-0">
        <div className="text-xl sm:text-2xl font-bold text-gray-800 dark:text-gray-100 tnum truncate">{value}</div>
        <div className="text-sm text-gray-400 truncate">{label}</div>
        {hint && <div className="text-xs text-gray-400 mt-0.5 truncate">{hint}</div>}
      </div>
    </Comp>
  );
}
