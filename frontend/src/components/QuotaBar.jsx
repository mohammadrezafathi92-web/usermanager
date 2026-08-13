import React from "react";
import { formatBytes } from "../utils.js";
import { useLanguage } from "../context/LanguageContext.jsx";

/**
 * Per-service usage bar. Deliberately per-service, never an aggregate across
 * unrelated purchases - see UserDetail.jsx and services/purchase_migration.py
 * for why a single combined bar over several independent services was wrong.
 *
 * `showPercent` adds the numeric percentage; the colour alone (amber >70%,
 * red >90%) is not something a colour-blind user can read, and it is the
 * signal that decides whether a customer is about to be cut off.
 */
export default function QuotaBar({ used, total, showPercent = true }) {
  const { t } = useLanguage();
  const unlimited = !total;
  const pct = unlimited ? 0 : Math.min(100, Math.round((used / total) * 100));
  const color = pct > 90 ? "bg-red-500" : pct > 70 ? "bg-amber-500" : "bg-brand-600";

  return (
    <div className="w-full">
      <div className="flex justify-between items-baseline gap-2 text-xs text-gray-500 mb-1">
        <span className={`tnum ${unlimited ? "font-medium text-gray-700" : ""}`}>{formatBytes(used)}</span>
        <span className="tnum">
          {unlimited ? (
            t("userDetail.unlimited")
          ) : (
            <>
              {showPercent && <span className={pct > 90 ? "text-red-500 font-medium" : "text-gray-400"}>٪{pct} </span>}
              {formatBytes(total)}
            </>
          )}
        </span>
      </div>
      <div
        className="h-2 rounded-full bg-gray-100 dark:bg-slate-800 overflow-hidden"
        role="progressbar"
        aria-valuenow={unlimited ? undefined : pct}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        {!unlimited && <div className={`h-full ${color} transition-all`} style={{ width: `${pct}%` }} />}
        {unlimited && <div className="h-full bg-gray-200 dark:bg-slate-700 w-1/4" />}
      </div>
    </div>
  );
}
