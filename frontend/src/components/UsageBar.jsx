import React from "react";

/**
 * Small horizontal usage bar (CPU/RAM/disk %) - color shifts from
 * brand/emerald "fine" up through amber/red as it approaches full, same
 * at-a-glance severity cue QuotaBar.jsx uses for per-service quota.
 *
 * Shared by Dashboard.jsx (bare bar, for the system CPU/RAM/disk tiles)
 * and Nodes.jsx's ResourceRow (bar + icon/label/text, per-node monitor
 * row) - previously each page defined its own copy of this exact
 * percent-to-color logic (design review, 2026-09).
 */
export default function UsageBar({ percent, className = "w-full" }) {
  const pct = Math.max(0, Math.min(100, percent || 0));
  const color = pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-amber-500" : "bg-emerald-500";
  return (
    <div className={`h-1.5 rounded-full bg-gray-100 dark:bg-slate-800 overflow-hidden ${className}`}>
      <div className={`h-full rounded-full ${color} transition-all`} style={{ width: `${pct}%` }} />
    </div>
  );
}
