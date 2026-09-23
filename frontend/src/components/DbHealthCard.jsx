import React, { useState } from "react";
import { Database, RefreshCw, ChevronDown, CheckCircle2, Sparkles } from "lucide-react";
import { runDbHealthCheck, optimizeDatabase } from "../api/client.js";
import { formatDateTime, formatBytes } from "../utils.js";

/**
 * "بررسی سلامت دیتابیس" - runs services/db_health.py's report on demand
 * (never automatically - it walks several tables' worth of ids, so it's a
 * deliberate action, not something to run on every page load). The report
 * itself stays read-only - see that module's docstring for why a mismatch
 * is reported, never auto-repaired.
 *
 * "بهینه‌سازی دیتابیس" (added 2026-09-23) is the one action this card does
 * take: deletes stale/expired log rows and VACUUMs the database - a
 * deliberately narrow, safe subset of what the report above can surface.
 * See services/db_health.py's optimize_database docstring for the exact
 * scope (never touches Users/Connections/Purchases or anything with
 * financial meaning).
 */
export default function DbHealthCard({ t, language }) {
  const [report, setReport] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(null);

  const [optimizing, setOptimizing] = useState(false);
  const [optimizeResult, setOptimizeResult] = useState(null);
  const [optimizeError, setOptimizeError] = useState("");

  const run = () => {
    setRunning(true);
    setError("");
    runDbHealthCheck()
      .then((res) => setReport(res.data))
      .catch((err) => setError(err?.response?.data?.detail || t("dbHealth.error")))
      .finally(() => setRunning(false));
  };

  const optimize = () => {
    setOptimizing(true);
    setOptimizeError("");
    setOptimizeResult(null);
    optimizeDatabase()
      .then((res) => setOptimizeResult(res.data))
      .catch((err) => setOptimizeError(err?.response?.data?.detail || t("dbHealth.optimizeError")))
      .finally(() => setOptimizing(false));
  };

  return (
    <div className="card mb-4">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="w-7 h-7 rounded-lg bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400 flex items-center justify-center shrink-0">
            <Database size={15} />
          </span>
          <h3 className="font-bold text-gray-700">{t("dbHealth.title")}</h3>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" className="btn-secondary" disabled={optimizing} onClick={optimize}>
            <Sparkles size={16} className={optimizing ? "animate-pulse" : ""} />
            {optimizing ? t("dbHealth.optimizing") : t("dbHealth.optimizeButton")}
          </button>
          <button type="button" className="btn-primary" disabled={running} onClick={run}>
            <RefreshCw size={16} className={running ? "animate-spin" : ""} />
            {running ? t("dbHealth.running") : t("dbHealth.runButton")}
          </button>
        </div>
      </div>
      <p className="text-xs text-gray-400 mb-4">{t("dbHealth.subtitle")}</p>

      {optimizeError && <div className="text-sm text-red-500 bg-red-50 dark:bg-red-500/10 dark:text-red-400 rounded-lg px-3 py-2 mb-4">{optimizeError}</div>}

      {optimizeResult && (
        <div className="text-sm text-emerald-600 bg-emerald-50 dark:bg-emerald-500/10 dark:text-emerald-400 rounded-lg px-3 py-3 mb-4 space-y-1">
          <div className="flex items-center gap-2">
            <CheckCircle2 size={16} />
            {t("dbHealth.optimizeDone", { count: optimizeResult.total_deleted })}
          </div>
          {optimizeResult.freed_bytes > 0 && (
            <div className="text-xs opacity-80">
              {t("dbHealth.optimizeFreed", { size: formatBytes(optimizeResult.freed_bytes) })}
            </div>
          )}
        </div>
      )}

      {error && <div className="text-sm text-red-500 bg-red-50 dark:bg-red-500/10 dark:text-red-400 rounded-lg px-3 py-2 mb-4">{error}</div>}

      {report && (
        <div className="space-y-3">
          <div className="text-xs text-gray-400">
            {t("dbHealth.checkedAt", { value: formatDateTime(report.checked_at, language) })}
          </div>

          {report.healthy ? (
            <div className="flex items-center gap-2 text-sm text-emerald-600 bg-emerald-50 dark:bg-emerald-500/10 dark:text-emerald-400 rounded-lg px-3 py-3">
              <CheckCircle2 size={18} />
              {t("dbHealth.allClear")}
            </div>
          ) : (
            <>
              <div className="flex items-center gap-2 text-sm">
                {report.error_count > 0 && (
                  <span className="badge-danger">
                    {t("dbHealth.errorCount", { count: report.error_count })}
                  </span>
                )}
                {report.warning_count > 0 && (
                  <span className="badge-warn">
                    {t("dbHealth.warningCount", { count: report.warning_count })}
                  </span>
                )}
              </div>

              <div className="space-y-2">
                {report.issues.map((issue) => (
                  <div key={issue.category} className="border border-gray-100 rounded-xl overflow-hidden">
                    <button
                      type="button"
                      className="w-full flex items-center justify-between px-4 py-3 text-start hover:bg-gray-50/60"
                      onClick={() => setExpanded((e) => (e === issue.category ? null : issue.category))}
                    >
                      <div className="flex items-center gap-2">
                        <span className={issue.severity === "error" ? "badge-danger" : "badge-warn"}>
                          {issue.severity === "error" ? t("dbHealth.severityError") : t("dbHealth.severityWarning")}
                        </span>
                        <span className="text-sm text-gray-700">{issue.title}</span>
                      </div>
                      <ChevronDown
                        size={16}
                        className={`text-gray-400 transition-transform ${expanded === issue.category ? "rotate-180" : ""}`}
                      />
                    </button>
                    {expanded === issue.category && issue.examples?.length > 0 && (
                      <div className="border-t border-gray-50 bg-gray-50/60 px-4 py-3">
                        <div className="text-xs text-gray-400 mb-2">
                          {t("dbHealth.examplesHint", { shown: issue.examples.length, total: issue.count })}
                        </div>
                        <div className="overflow-x-auto">
                          <pre className="text-xs font-mono text-gray-600 whitespace-pre-wrap" dir="ltr">
                            {issue.examples.map((ex) => JSON.stringify(ex)).join("\n")}
                          </pre>
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
