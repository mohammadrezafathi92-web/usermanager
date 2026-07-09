import React from "react";
import { formatBytes } from "../utils.js";

export default function QuotaBar({ used, total }) {
  const unlimited = !total;
  const pct = unlimited ? 0 : Math.min(100, Math.round((used / total) * 100));
  const color = pct > 90 ? "bg-red-500" : pct > 70 ? "bg-amber-500" : "bg-brand-600";

  return (
    <div className="w-full">
      <div className="flex justify-between text-xs text-gray-500 mb-1">
        <span className={unlimited ? "font-medium text-gray-700" : ""}>{formatBytes(used)}</span>
        <span>{unlimited ? "نامحدود" : formatBytes(total)}</span>
      </div>
      <div className="h-2 rounded-full bg-gray-100 overflow-hidden">
        {!unlimited && (
          <div className={`h-full ${color} transition-all`} style={{ width: `${pct}%` }} />
        )}
        {unlimited && <div className="h-full bg-gray-200 w-1/4" />}
      </div>
    </div>
  );
}
