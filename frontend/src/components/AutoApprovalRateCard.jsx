import React, { useEffect, useId, useRef, useState } from "react";
import { Timer } from "lucide-react";
import { fetchApprovalRuntime, updateAutoApprovalRateMode } from "../api/client.js";

/**
 * Automatic receipt approval, in one place (superadmin): what the bot's
 * auto-approve switch and its rules say as last SAVED (`summary`, from the
 * bot settings card right above), and the separate per-customer limit of one
 * automatic approval per rolling 60 minutes. The limit is a runtime mode of
 * the durable approval path, saved on its own with a second confirmation;
 * it is not a schedule for receipts that are waiting.
 */
export default function AutoApprovalRateCard({ t, summary }) {
  const [runtime, setRuntime] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [autoMode, setAutoMode] = useState("off");
  const [saved, setSaved] = useState(false);
  const inFlight = useRef(false);
  const autoId = useId();

  const fail = (err) => {
    const detail = err?.response?.data?.detail;
    setError(typeof detail === "string" ? detail : detail?.error || detail?.code || t("wallet.error"));
  };

  const load = () => fetchApprovalRuntime()
    .then((res) => {
      setRuntime(res.data);
      setAutoMode(res.data.requested_auto_rate_limit_mode || "off");
      return true;
    })
    .catch((err) => { fail(err); return false; });

  useEffect(() => { load(); }, []);

  const canEditAuto = runtime?.effective_registration_mode === "required" && runtime?.key_identity_ready === true;
  const requestedAuto = runtime?.requested_auto_rate_limit_mode || "off";
  const effectiveAuto = runtime?.effective_auto_rate_limit_mode || "off";

  const saveAutoMode = () => {
    if (!canEditAuto || inFlight.current || !window.confirm(t("wallet.autoConfirm", {
      value: t(`wallet.autoMode.${autoMode}`),
    }))) return;
    inFlight.current = true;
    setBusy(true); setError(""); setSaved(false);
    updateAutoApprovalRateMode(autoMode).then(async () => {
      const refreshed = await load();
      setSaved(refreshed);
    }).catch(fail).finally(() => { inFlight.current = false; setBusy(false); });
  };

  const hours = (h) => `${String(h ?? 0).padStart(2, "0")}:00`;

  return (
    <div className="card mb-4">
      <div className="flex items-center gap-2 mb-2">
        <span className="w-7 h-7 rounded-lg bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400 flex items-center justify-center shrink-0">
          <Timer size={15} />
        </span>
        <h3 className="font-bold text-gray-700">{t("settings.autoApprovalStateTitle")}</h3>
      </div>

      <section aria-label={t("settings.autoApprovalSavedRules")} className="text-sm space-y-1 mb-4">
        <h4 className="font-bold text-gray-700 dark:text-gray-200">{t("settings.autoApprovalSavedRules")}</h4>
        {summary == null ? (
          <p className="text-gray-500">{t("settings.autoApprovalSummaryLoading")}</p>
        ) : !summary.auto_approve_enabled ? (
          <p>{t("settings.autoApprovalOff")}</p>
        ) : (
          <ul className="list-disc ps-5 space-y-1 text-gray-700 dark:text-gray-200">
            <li>{t("settings.autoApprovalOn")}</li>
            <li>{summary.auto_approve_ignore_hours ? t("settings.autoApprovalAllHours")
              : t("settings.autoApprovalHours", { from: hours(summary.auto_approve_from_hour ?? 9), to: hours(summary.auto_approve_to_hour ?? 23) })}</li>
            <li>{summary.auto_approve_max_amount ? t("settings.autoApprovalMax", { amount: Number(summary.auto_approve_max_amount).toLocaleString() })
              : t("settings.autoApprovalNoMax")}</li>
            <li>{summary.auto_approve_returning_only !== false ? t("settings.autoApprovalReturning") : t("settings.autoApprovalAnyCustomer")}</li>
          </ul>
        )}
      </section>

      <section aria-labelledby={`${autoId}-title`} className="border-t border-gray-200 dark:border-gray-700 pt-4 space-y-3 text-gray-800 dark:text-gray-100">
        <h4 id={`${autoId}-title`} className="font-bold text-gray-700 dark:text-gray-200">{t("wallet.autoTitle")}</h4>
        <p id={`${autoId}-help`} className="text-sm text-gray-600 dark:text-gray-300 leading-relaxed">{t("wallet.autoHelp")}</p>
        <p className="text-sm text-gray-600 dark:text-gray-300 leading-relaxed">{t("wallet.autoScope")}</p>
        {error && <div role="alert" className="text-sm text-red-600 bg-red-50 dark:bg-red-500/10 dark:text-red-400 rounded-lg px-3 py-2">{error}</div>}
        <dl className="text-sm grid gap-2 sm:grid-cols-2" aria-live="polite">
          <div><dt className="text-gray-600 dark:text-gray-300">{t("wallet.autoRequested")}</dt><dd className="font-medium">{runtime ? t(`wallet.autoMode.${requestedAuto}`) : "—"}</dd></div>
          <div><dt className="text-gray-600 dark:text-gray-300">{t("wallet.autoEffective")}</dt><dd className="font-medium">{runtime ? t(`wallet.autoMode.${effectiveAuto}`) : "—"}</dd></div>
        </dl>
        {runtime && !canEditAuto && <p id={`${autoId}-blocked`} className="text-sm text-amber-700 dark:text-amber-300">{t("wallet.autoBlocked")}</p>}
        <div className="flex flex-wrap items-end gap-3" aria-busy={busy}>
          <div className="flex flex-col gap-1 w-full sm:w-auto sm:min-w-52">
            <label htmlFor={autoId} className="text-sm font-medium">{t("wallet.autoSelect")}</label>
            <select id={autoId} className="input min-h-11 disabled:opacity-60 disabled:cursor-not-allowed"
              aria-describedby={`${autoId}-help${runtime && !canEditAuto ? ` ${autoId}-blocked` : ""}`}
              disabled={busy || !canEditAuto} value={autoMode}
              onChange={(event) => { setAutoMode(event.target.value); setSaved(false); }}>
              {["off", "shadow", "enforced"].map((mode) => <option key={mode} value={mode}>{t(`wallet.autoMode.${mode}`)}</option>)}
            </select>
          </div>
          <button type="button" className="btn-primary min-h-11" disabled={busy || !canEditAuto || autoMode === requestedAuto}
            onClick={saveAutoMode}>{busy ? t("wallet.autoSaving") : t("wallet.autoSave")}</button>
        </div>
        {saved && <p role="status" className="text-sm text-emerald-700 dark:text-emerald-300">{t("wallet.autoSaved")}</p>}
      </section>
    </div>
  );
}
