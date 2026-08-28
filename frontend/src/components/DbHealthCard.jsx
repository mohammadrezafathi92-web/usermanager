import React, { useState } from "react";
import { Database, RefreshCw, ChevronDown, CheckCircle2 } from "lucide-react";
import { runDbHealthCheck } from "../api/client.js";
import { formatDateTime } from "../utils.js";

/**
 * "بررسی سلامت دیتابیس" - runs services/db_health.py's report on demand
 * (never automatically - it walks several tables' worth of ids, so it's a
 * deliberate action, not something to run on every page load). Read-only;
 * there is nothing here to "fix" from the panel - see that module's
 * docstring for why a mismatch is reported, never auto-repaired.
 */
export default function DbHealthCard({ t, language }) {
  const [report, setReport] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(null);

  const run = () => {
    setRunning(true);
    setError("");
    runDbHealthCheck()
      .then((res) => setReport(res.data))
      .catch((err) => setError(err?.response?.data?.detail || t("dbHealth.error")))
      .finally(() => setRunning(false));
  };

  return (
    <div className="card mb-4">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <Database size={18} className="text-brand-600" />
          <h3 className="font-bold text-gray-700">{t("dbHealth.title")}</h3>
        </div>
        <button type="button" className="btn-primary" disabled={running} onClick={run}>
          <RefreshCw size={16} className={running ? "animate-spin" : ""} />
          {running ? t("dbHealth.running") : t("dbHealth.runButton")}
        </button>
      </div>
      <p className="text-xs text-gray-400 mb-4">{t("dbHealth.subtitle")}</p>

      {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2 mb-4">{error}</div>}

      {report && (
        <div className="space-y-3">
          <div className="text-xs text-gray-400">
            {t("dbHealth.checkedAt", { value: formatDateTime(report.checked_at, language) })}
          </div>

          {report.healthy ? (
            <div className="flex items-center gap-2 text-sm text-emerald-600 bg-emerald-50 rounded-lg px-3 py-3">
              <CheckCircle2 size={18} />
              {t("dbHealth.allClear")}
            </div>
          ) : (
            <>
              <div className="flex items-center gap-2 text-sm">
                {report.error_count > 0 && (
                  <span className="badge bg-red-50 text-red-600">
                    {t("dbHealth.errorCount", { count: report.error_count })}
                  </span>
                )}
                {report.warning_count > 0 && (
                  <span className="badge bg-amber-50 text-amber-600">
                    {t("dbHealth.warningCount", { count: report.warning_count })}
                  </span>
                )}
              </div>

              <div className="space-y-2">
                {report.issues.map((issue) => (
                  <div key={issue.category} className="border border-gray-100 rounded-xl overflow-hidden">
                    <button
                      type="button"
                      className="w-full flex items-center justify-between px-4 py-3 text-right hover:bg-gray-50/60"
                      onClick={() => setExpanded((e) => (e === issue.category ? null : issue.category))}
                    >
                      <div className="flex items-center gap-2">
                        <span className={`badge ${issue.severity === "error" ? "bg-red-50 text-red-600" : "bg-amber-50 text-amber-600"}`}>
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
